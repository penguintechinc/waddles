"""Analytics exclusions on a REAL migrated Postgres (alembic 0129 `data_use_exclusions`).

Proves what sqlite cannot: the same-transaction guarantee (a failing consent write leaves no
exclusion; a failing exclusion leaves no consent write), the `ON CONFLICT` / uuid-typed SQL, the
reconciler's CTE batches, its idempotence, the erasure-ledger source, and that a second replica
holding the advisory lock makes a pass skip. Skipped (never failed) without the docker CLI.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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

from services import analytics_exclusions_service as ex  # noqa: E402
from services import audit_http  # noqa: E402
from services import cookie_consent_service as consent  # noqa: E402
from services.analytics_exclusions_service import ExclusionError  # noqa: E402
from services.analytics_exclusions_service import ExclusionReason as R  # noqa: E402
from services.schema import bind_auth_tables, bind_privacy_tables  # noqa: E402

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_DDL = """
CREATE TABLE IF NOT EXISTS cookie_consent (
    id SERIAL PRIMARY KEY, user_id INTEGER, consent_id VARCHAR(255) NOT NULL UNIQUE,
    preferences JSON, consent_version VARCHAR(50) NOT NULL,
    consent_method VARCHAR(50) DEFAULT 'banner', ip_address VARCHAR(45), user_agent TEXT,
    consented_at TIMESTAMP, updated_at TIMESTAMP, expires_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS cookie_audit_log (
    id SERIAL PRIMARY KEY, consent_id VARCHAR(255), user_id INTEGER,
    action VARCHAR(50) NOT NULL, category VARCHAR(50), previous_value BOOLEAN,
    new_value BOOLEAN, consent_version VARCHAR(50), ip_address VARCHAR(45), user_agent TEXT,
    created_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS data_deletion_requests (
    id SERIAL PRIMARY KEY, hub_user_id INTEGER, requested_at TIMESTAMP, completed_at TIMESTAMP,
    status VARCHAR(20), error_detail TEXT, deletion_scope JSON);
"""


@pytest.fixture(scope="module")
def pg() -> Iterator[PgTestDatabase]:
    with migrated_postgres(f"analytics-excl-{os.getpid()}") as db:
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        conn.cursor().execute(_DDL)
        conn.close()
        yield db


@pytest.fixture
def adal(pg: PgTestDatabase) -> Iterator[AsyncDAL]:
    conn = psycopg2.connect(pg.dsn)
    conn.autocommit = True
    conn.cursor().execute(
        "TRUNCATE data_use_exclusions, cookie_consent, cookie_audit_log, data_deletion_requests"
    )
    conn.close()
    async_dal = AsyncDAL(pg.dsn.replace("postgresql://", "postgres://", 1), pool_size=2)
    bind_auth_tables(async_dal.dal, migrate=False)
    bind_privacy_tables(async_dal.dal)
    yield async_dal
    async_dal.dal.close()


@pytest.fixture(autouse=True)
def _audit(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    service = MagicMock()
    service.record = AsyncMock(return_value=None)
    monkeypatch.setattr(audit_http, "get_service", lambda: service)
    return service


def _user(adal: AsyncDAL, name: str) -> tuple[int, str]:
    rows = adal.dal.executesql(
        "INSERT INTO hub_users (username) VALUES (%s) RETURNING id, uuid::text",
        placeholders=[f"{name}-{os.urandom(3).hex()}"],
    )
    adal.dal.commit()
    return int(rows[0][0]), str(rows[0][1])


def _reasons(adal: AsyncDAL, token: str) -> set[int]:
    got = set(ex.exclusion_reasons(adal.dal, token))
    adal.dal.commit()
    return got


async def _save(
    adal: AsyncDAL, uid: int, prefs: dict[str, Any], cid: str, gpc: bool = False
) -> None:
    await consent.save_consent(
        adal,
        adal.dal,
        user_id=uid,
        consent_id=cid,
        preferences=prefs,
        consent_method="banner",
        ip_address=None,
        user_agent=None,
        version="1.0",
        gpc_applied=gpc,
    )


async def test_consent_and_exclusion_commit_together(adal: AsyncDAL) -> None:
    uid, token = _user(adal, "a")
    await _save(adal, uid, {"doNotSell": True}, "pg-a", gpc=True)
    assert _reasons(adal, token) == {1, 2}
    # idempotent replay (ON CONFLICT path) inserts nothing new
    await _save(adal, uid, {"doNotSell": True}, "pg-a", gpc=True)
    assert _reasons(adal, token) == {1, 2}


async def test_failed_consent_write_leaves_no_exclusion(adal: AsyncDAL) -> None:
    uid, token = _user(adal, "b")

    def _boom(d: Any) -> None:
        d.executesql("SELECT 1/0")  # real Postgres error mid-transaction

    with pytest.raises(Exception):  # noqa: B017 - driver DivisionByZero
        await ex.apply_consent_change(
            adal, adal.dal, user_id=uid, write=_boom, previous=None, merged={"doNotSell": True}
        )
    assert _reasons(adal, token) == set()


async def test_failed_exclusion_rolls_back_the_consent_write(adal: AsyncDAL) -> None:
    with pytest.raises(ExclusionError):
        await _save(adal, 2_000_000_000, {"doNotSell": True}, "pg-ghost")
    rows = adal.dal.executesql("SELECT count(*) FROM cookie_consent WHERE consent_id='pg-ghost'")
    adal.dal.commit()
    assert rows[0][0] == 0


async def test_regrant_removes_only_its_reason_and_never_erased(adal: AsyncDAL) -> None:
    uid, token = _user(adal, "c")
    await _save(adal, uid, {"doNotSell": True}, "pg-c", gpc=True)
    ex.add_exclusion(adal.dal, token, R.ERASED)
    ex.add_exclusion(adal.dal, token, R.AGE_NOT_ADULT)
    adal.dal.commit()
    await _save(adal, uid, {"doNotSell": False}, "pg-c")
    assert _reasons(adal, token) == {4, 5}


async def test_reconcile_rebuilds_missing_rows_idempotently(adal: AsyncDAL) -> None:
    dns_id, dns_token = _user(adal, "dns")
    erased_id, erased_token = _user(adal, "erased")
    clear_id, clear_token = _user(adal, "clear")
    for cid, uid, prefs in (
        ("r1", dns_id, '{"doNotSell": true}'),
        ("r2", clear_id, '{"doNotSell": false}'),
    ):
        adal.dal.executesql(
            "INSERT INTO cookie_consent (user_id, consent_id, preferences, consent_version) "
            "VALUES (%s, %s, %s, '1.0')",
            placeholders=[uid, cid, prefs],
        )
    adal.dal.executesql(
        "INSERT INTO data_deletion_requests (hub_user_id, status) VALUES (%s, 'completed'), "
        "(%s, 'failed')",
        placeholders=[erased_id, clear_id],
    )
    adal.dal.commit()

    first = ex.reconcile_exclusions(adal.dal, batch_size=1)  # forces multi-batch paging
    assert first.consent_examined == 2 and first.ledger_examined == 1
    assert first.added == {R.DO_NOT_SELL: 1, R.ERASED: 1} and first.batches >= 4
    assert _reasons(adal, dns_token) == {1}
    assert _reasons(adal, erased_token) == {4}
    assert _reasons(adal, clear_token) == set()

    second = ex.reconcile_exclusions(adal.dal)
    assert second.total_added == 0 and second.consent_examined == 2
    assert _reasons(adal, dns_token) == {1}


async def test_reconcile_never_undoes_a_regrant(adal: AsyncDAL) -> None:
    uid, token = _user(adal, "d")
    await _save(adal, uid, {"doNotSell": True}, "pg-d")
    await _save(adal, uid, {"doNotSell": False}, "pg-d")
    ex.reconcile_exclusions(adal.dal)
    assert _reasons(adal, token) == set()


async def test_second_replica_holding_the_lock_makes_the_pass_skip(
    adal: AsyncDAL, pg: PgTestDatabase
) -> None:
    holder = psycopg2.connect(pg.dsn)
    try:
        cur = holder.cursor()
        cur.execute("SELECT pg_advisory_lock(%s)", (ex._ADVISORY_LOCK_KEY,))  # noqa: SLF001
        result = ex.reconcile_exclusions(adal.dal)
        assert result.skipped_locked is True and result.batches == 0
    finally:
        holder.close()
    assert ex.reconcile_exclusions(adal.dal).skipped_locked is False


async def test_reconcile_once_is_flag_gated_and_audited(
    adal: AsyncDAL, monkeypatch: pytest.MonkeyPatch, _audit: MagicMock
) -> None:
    uid, token = _user(adal, "e")
    adal.dal.executesql(
        "INSERT INTO cookie_consent (user_id, consent_id, preferences, consent_version) "
        "VALUES (%s, 'r-e', '{\"doNotSell\": true}', '1.0')",
        placeholders=[uid],
    )
    adal.dal.commit()
    seen: list[str] = []

    async def _flag(key: str, **_kw: Any) -> bool:
        seen.append(key)
        return False

    monkeypatch.setattr(ex, "feature_enabled", _flag)
    assert await ex.run_reconcile_once(adal) is None
    assert seen == ["waddles.analytics-exclusions-reconcile"] and _reasons(adal, token) == set()

    async def _on(key: str, **_kw: Any) -> bool:
        return True

    monkeypatch.setattr(ex, "feature_enabled", _on)
    result = await ex.run_reconcile_once(adal)
    assert result is not None and result.total_added == 1
    assert _reasons(adal, token) == {1}
    assert _audit.record.await_args.args[0].action == "privacy.exclusions_reconciled"
