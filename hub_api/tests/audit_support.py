"""Shared helpers + fixtures for the tamper-evident audit-log tests (GRC audit finding #3).

Test modules import the fixtures they need (``from tests.audit_support import audit_dal,
audit_service``) rather than this living in the 1800-line shared ``conftest.py``.

``audit_dal`` extends the existing ``install_dal`` fixture (file-backed sqlite shared with the
pydal ``bundle_install_db``, which already owns ``tenants`` and the legacy ``audit_log``) with the
two tables the audit service reads/writes: ``audit_events`` -- the sqlite-compatible mirror of
migration 0048's table, minus the Postgres-only triggers/CHECKs the real-Postgres test proves --
and a ``hub_users`` table carrying the ``uuid`` column migration 0043 adds.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    insert,
)

from services.audit_chain import GENESIS_HASH, ChainRecord, canonical_timestamp, seal
from services.audit_events import (
    ActorKind,
    AuditAction,
    AuditCategory,
    AuditEvent,
    AuditOutcome,
)
from services.audit_service import AuditService

USER_ID = 7
USER_UUID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER_ID = 8
OTHER_USER_UUID = uuid.UUID("22222222-2222-4222-8222-222222222222")


def create_audit_tables(conn: Any) -> None:
    """Synchronous Core DDL: sqlite mirror of 0048's `audit_events` plus `hub_users`(uuid)."""
    metadata = MetaData()
    Table(
        "audit_events",
        metadata,
        Column("chain_id", String(64), primary_key=True),
        Column("seq", BigInteger, primary_key=True, autoincrement=False),
        Column("event_id", String(36), nullable=False),
        Column("occurred_at", DateTime, nullable=False),
        Column("actor_uuid", String(36)),
        Column("actor_kind", String(16), nullable=False),
        Column("category", String(16), nullable=False),
        Column("action", String(100), nullable=False),
        Column("outcome", String(16), nullable=False),
        Column("target_type", String(50)),
        Column("target_id", String(128)),
        Column("details", JSON, nullable=False),
        Column("prev_hash", String(64), nullable=False),
        Column("record_hash", String(64), nullable=False),
        Column("hash_version", String(16), nullable=False),
        UniqueConstraint("event_id"),
        UniqueConstraint("chain_id", "prev_hash"),
        UniqueConstraint("record_hash"),
    )
    Table(
        "hub_users",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("uuid", String(36), unique=True),
        Column("username", String(255)),
    )
    metadata.create_all(conn)


@dataclass(slots=True)
class FakeGate:
    """Controllable entitlement gate: records every slug it was asked about."""

    entitled: bool | set[str] = True
    calls: list[str] = field(default_factory=list)

    async def __call__(self, tenant_slug: str) -> bool:
        """Answer for ``tenant_slug`` -- a bool for everyone, or membership in a slug set."""
        self.calls.append(tenant_slug)
        if isinstance(self.entitled, bool):
            return self.entitled
        return tenant_slug in self.entitled


@pytest.fixture
async def audit_dal(install_dal: Any, bundle_install_db: Any) -> Any:
    """`install_dal` + `audit_events` + `hub_users`(uuid), with two users and a 2nd tenant."""
    async with install_dal.engine.begin() as conn:
        await conn.run_sync(create_audit_tables)
        users = Table("hub_users", MetaData(), Column("id", Integer), Column("uuid", String(36)))
        await conn.execute(
            insert(users),
            [
                {"id": USER_ID, "uuid": str(USER_UUID)},
                {"id": OTHER_USER_ID, "uuid": str(OTHER_USER_UUID)},
            ],
        )
    bundle_install_db.dal.tenants.insert(slug="other-co", display_name="Other Co", is_active=True)
    bundle_install_db.dal.commit()
    await install_dal.reflect()
    return install_dal


@pytest.fixture
def gate() -> FakeGate:
    """An entitled-for-everyone gate (tests flip ``gate.entitled`` to exercise the skip path)."""
    return FakeGate()


@pytest.fixture
def audit_service(audit_dal: Any, gate: FakeGate) -> AuditService:
    """A fresh `AuditService` on the shared sqlite DB with the controllable gate."""
    return AuditService(audit_dal, gate=gate)


def make_event(
    *,
    action: str = AuditAction.ADMIN_ACTION,
    category: AuditCategory = AuditCategory.ADMIN,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    user_id: int | None = USER_ID,
    tenant_id: int | None = 1,
    tenant_slug: str | None = None,
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    """A valid event (default: a user-driven admin action on tenant 1)."""
    return AuditEvent(
        category=category,
        action=str(action),
        outcome=outcome,
        actor_kind=ActorKind.USER if user_id is not None else ActorKind.SYSTEM,
        actor_user_id=user_id,
        tenant_id=tenant_id,
        tenant_slug=tenant_slug,
        target_type="http_route",
        target_id=target_id,
        details=details or {"method": "POST", "status": 200},
    )


def build_chain(
    count: int, *, chain_id: str = "tenant:1", start_seq: int = 1, prev_hash: str = GENESIS_HASH
) -> list[ChainRecord]:
    """A valid in-memory chain of ``count`` sealed records (no database)."""
    records: list[ChainRecord] = []
    previous = prev_hash
    for offset in range(count):
        seq = start_seq + offset
        sealed = seal(
            ChainRecord(
                chain_id=chain_id,
                seq=seq,
                event_id=str(uuid.UUID(int=seq + 1000)),
                occurred_at=canonical_timestamp(_ts(seq)),
                actor_uuid=str(USER_UUID),
                actor_kind="user",
                category="admin",
                action="admin.action",
                outcome="success",
                target_type="http_route",
                target_id=None,
                details={"method": "POST", "status": 200, "seq_marker": seq},
                prev_hash=previous,
                record_hash="",
            )
        )
        records.append(sealed)
        previous = sealed.record_hash
    return records


def _ts(seq: int) -> Any:
    """Deterministic, strictly increasing UTC timestamps for built chains."""
    from datetime import UTC, datetime, timedelta

    return datetime(2026, 10, 10, 12, 0, 0, 123456, tzinfo=UTC) + timedelta(seconds=seq)


def replace_record(record: ChainRecord, **changes: Any) -> ChainRecord:
    """Copy of ``record`` with fields overridden (the dataclass is frozen)."""
    return dataclasses.replace(record, **changes)


Mutation = Callable[[ChainRecord], ChainRecord]
