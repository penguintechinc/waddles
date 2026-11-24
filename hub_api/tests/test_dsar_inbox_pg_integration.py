"""DSAR inbox service on REAL Postgres 17 (alembic 0121 applied), through the production DAL.

The sqlite suite (`test_dsar_inbox.py`) proves the logic; this proves what only a real server can:
`UUID`/`TIMESTAMPTZ`/`JSONB`/`BIGINT` columns round-trip through pydal, `COALESCE(extended_until,
due_at)` due-state filtering, the single-use token claim, expiry + contact purge, and that the
append-only trigger lets the service write events. Skipped (never failed) without the docker CLI.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from flask_core.database import AsyncDAL

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from services import dsar_service as svc  # noqa: E402
from services.schema import bind_dsar_tables  # noqa: E402

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

KEY = "cd" * 32


@pytest.fixture(scope="module")
def pg() -> Iterator[PgTestDatabase]:
    with migrated_postgres(f"dsar-svc-{os.getpid()}") as db:
        yield db


@pytest.fixture(scope="module")
def tenant_id(pg: PgTestDatabase) -> int:
    conn = psycopg2.connect(pg.dsn)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO tenants (slug, is_active) VALUES ('pg-dsar', TRUE) RETURNING id")
        return int(cur.fetchone()[0])
    finally:
        conn.close()


@pytest.fixture
def dal_pair(pg: PgTestDatabase, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("DSAR_FINGERPRINT_KEY", KEY)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "ab" * 32)
    monkeypatch.setenv("DSAR_VERIFY_URL_BASE", "https://x.example/v")
    url = pg.dsn.replace("postgresql://", "postgres://", 1)
    async_dal = AsyncDAL(url, pool_size=1)
    bind_dsar_tables(async_dal.dal, migrate=False)
    yield async_dal
    async_dal.dal.close()


async def test_public_flow_filters_expiry_and_events_on_real_postgres(
    dal_pair: Any, tenant_id: int
) -> None:
    dal = dal_pair.dal
    row, token = await svc.create_request(
        dal_pair,
        dal,
        tenant_id=tenant_id,
        request_type=svc.RequestType.EXPORT,
        regime=svc.Regime.CCPA,
        source=svc.Source.PUBLIC_FORM,
        actor_kind=svc.ActorKind.PUBLIC_REQUESTER,
        email="pg.user@example.org",
        message="hi",
    )
    assert (
        token and row.status == int(svc.RequestStatus.VERIFICATION_PENDING) and row.due_at is None
    )
    assert svc.decrypt_contact(row) == (None, None)  # unverified: never revealed

    assert await svc.consume_verification_token(dal_pair, dal, token) is True
    assert await svc.consume_verification_token(dal_pair, dal, token) is False  # single use
    verified = await svc.get_request(dal_pair, dal, tenant_id=tenant_id, public_id=row.public_id)
    assert verified.status == int(svc.RequestStatus.VERIFIED)
    assert svc.aware(verified.due_at) == svc.aware(verified.verified_at) + timedelta(days=45)
    assert svc.decrypt_contact(verified) == ("pg.user@example.org", "hi")

    soon, _ = await svc.create_request(
        dal_pair,
        dal,
        tenant_id=tenant_id,
        request_type=svc.RequestType.ERASE,
        regime=svc.Regime.GDPR,
        source=svc.Source.ADMIN_ENTERED,
        actor_kind=svc.ActorKind.ADMIN,
        requested_at=datetime.now(UTC) - timedelta(days=25),
    )
    overdue, _ = await svc.create_request(
        dal_pair,
        dal,
        tenant_id=tenant_id,
        request_type=svc.RequestType.OTHER,
        regime=svc.Regime.OTHER,
        source=svc.Source.ADMIN_ENTERED,
        actor_kind=svc.ActorKind.ADMIN,
        requested_at=datetime.now(UTC) - timedelta(days=40),
    )
    now = svc.now_utc()

    async def ids(state: str) -> set[str]:
        rows, _ = await svc.list_requests(
            dal_pair, dal, tenant_id=tenant_id, filters=svc.ListFilters(due_state=state), now=now
        )
        return {r.public_id for r in rows}

    assert await ids("ok") == {row.public_id}
    assert await ids("due_soon") == {soon.public_id}
    assert await ids("overdue") == {overdue.public_id}
    await svc.extend_request(
        dal_pair, dal, tenant_id=tenant_id, row=overdue, admin_user_id=0, reason="r"
    )
    assert await ids("overdue") == set()  # COALESCE(extended_until, due_at) honours the extension

    pending, _ = await svc.create_request(
        dal_pair,
        dal,
        tenant_id=tenant_id,
        request_type=svc.RequestType.EXPORT,
        regime=svc.Regime.GDPR,
        source=svc.Source.PUBLIC_FORM,
        actor_kind=svc.ActorKind.PUBLIC_REQUESTER,
        email="late@example.org",
    )
    result = await svc.run_maintenance(dal_pair, dal, now=now + timedelta(hours=73))
    assert result.expired == 1
    expired = await svc.get_request(dal_pair, dal, tenant_id=tenant_id, public_id=pending.public_id)
    assert expired.status == int(svc.RequestStatus.EXPIRED) and expired.contact_email_enc is None
    events = await svc.load_events(dal_pair, dal, tenant_id=tenant_id, request_id=int(pending.id))
    assert [e.event_type for e in events][-1] == int(svc.EventType.EXPIRED)
    assert (
        await svc.get_request(dal_pair, dal, tenant_id=tenant_id + 999, public_id=row.public_id)
    ) is None


async def test_atomic_intake_unique_index_and_compare_and_set_on_real_postgres(
    dal_pair: Any, tenant_id: int
) -> None:
    """Review fixes on real PG: one-transaction intake, partial unique index, microsecond CAS."""
    import asyncio

    from services import ban_suppression, dsar_outbox

    dal = dal_pair.dal
    kwargs: dict[str, Any] = {
        "tenant_id": tenant_id,
        "request_type": svc.RequestType.CORRECTION,
        "regime": svc.Regime.GDPR,
        "email": "atomic@example.org",
        "message": None,
    }

    def work() -> str:
        row, token = svc.create_public_request_sync(dal, **kwargs)
        dsar_outbox.enqueue_in_transaction(
            dal, request_row=row, token=token, resend=False, tenant_slug="pg-dsar"
        )
        return str(row.public_id)

    public_id = await ban_suppression.run_in_transaction(dal_pair, dal, work)
    outbox = await dal_pair.select_async(
        dal(dal.dsar_mail_outbox.tenant_id == tenant_id), dal.dsar_mail_outbox.request_id
    )
    created = await svc.get_request(dal_pair, dal, tenant_id=tenant_id, public_id=public_id)
    assert [int(o.request_id) for o in outbox] == [int(created.id)]

    with pytest.raises(Exception) as caught:  # the 0124 partial unique index, real driver error
        await ban_suppression.run_in_transaction(dal_pair, dal, work)
    assert svc.is_integrity_violation(caught.value)
    again = await dal_pair.select_async(dal(dal.dsar_requests.tenant_id == tenant_id))
    assert (
        sum(
            1 for r in again if r.contact_email_hash == created.contact_email_hash and r.status == 2
        )
        == 1
    )  # rolled back: still ONE open request, ONE outbox row
    assert len(await dal_pair.select_async(dal(dal.dsar_mail_outbox.tenant_id == tenant_id))) == 1

    # compare-and-set: of two admins holding the same read, exactly one claims
    admin_row, _ = await svc.create_request(
        dal_pair,
        dal,
        tenant_id=tenant_id,
        request_type=svc.RequestType.EXPORT,
        regime=svc.Regime.GDPR,
        source=svc.Source.ADMIN_ENTERED,
        actor_kind=svc.ActorKind.ADMIN,
    )
    results = await asyncio.gather(
        svc.claim_for_action(dal_pair, dal, tenant_id=tenant_id, row=admin_row),
        svc.claim_for_action(dal_pair, dal, tenant_id=tenant_id, row=admin_row),
        return_exceptions=True,
    )
    conflicts = [r for r in results if isinstance(r, svc.ApiError) and r.code == "DSAR_CONFLICT"]
    assert len(conflicts) == 1 and len([r for r in results if not isinstance(r, Exception)]) == 1
