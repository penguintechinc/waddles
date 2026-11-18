"""Global Do-Not-Sell writer on REAL Postgres: concurrent double-apply is idempotent, never aborts.

Skipped (never failed) without the docker CLI.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Iterator
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

from services import admin_data_privacy_service as svc  # noqa: E402
from services.schema import bind_privacy_tables  # noqa: E402

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)


_CONSENT_DDL = """
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
CREATE TABLE IF NOT EXISTS cookie_policy_versions (
    id SERIAL PRIMARY KEY, version VARCHAR(50) NOT NULL UNIQUE, content TEXT NOT NULL,
    changes_summary TEXT, is_active BOOLEAN DEFAULT FALSE, effective_date TIMESTAMP,
    created_by INTEGER, created_at TIMESTAMP);
"""


@pytest.fixture(scope="module")
def pg() -> Iterator[PgTestDatabase]:
    with migrated_postgres(f"admin-dns-{os.getpid()}") as db:
        yield db


@pytest.fixture
def ids(pg: PgTestDatabase) -> dict[str, int]:
    conn = psycopg2.connect(pg.dsn)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        # The consent tables come from hub-api's own bootstrap (not alembic); create them here.
        cur.execute(_CONSENT_DDL)
        out: dict[str, int] = {}
        for name in ("u1", "u2", "u3"):
            cur.execute("INSERT INTO hub_users DEFAULT VALUES RETURNING id")
            out[name] = int(cur.fetchone()[0])
        return out
    finally:
        conn.close()


@pytest.fixture
def adal(pg: PgTestDatabase) -> Iterator[Any]:
    async_dal = AsyncDAL(pg.dsn.replace("postgresql://", "postgres://", 1), pool_size=6)
    bind_privacy_tables(async_dal.dal)
    yield async_dal
    async_dal.dal.close()


def _opted_out(adal: Any, user_id: int) -> bool:
    rows = adal.dal(adal.dal.cookie_consent.user_id == user_id).select()
    adal.dal.commit()
    return bool(rows) and all(r.preferences.get("doNotSell") is True for r in rows)


async def test_concurrent_double_apply_never_aborts_on_postgres(
    adal: Any, ids: dict[str, int]
) -> None:
    # Existing consent row: concurrent updates of the same row must all succeed (idempotent).
    adal.dal.cookie_consent.insert(
        user_id=ids["u1"],
        consent_id=f"pg-dns-{os.getpid()}",
        preferences={"necessary": True, "marketing": True, "doNotSell": False},
        consent_version="1.0",
    )
    adal.dal.commit()

    outcomes = await asyncio.gather(
        *[svc.apply_do_not_sell(adal, adal.dal, user_id=ids["u1"]) for _ in range(12)]
    )

    assert any(outcomes)
    assert _opted_out(adal, ids["u1"])
    assert await svc.apply_do_not_sell(adal, adal.dal, user_id=ids["u1"]) is False


async def test_bulk_style_loop_survives_duplicates(adal: Any, ids: dict[str, int]) -> None:
    results = []
    for user in (ids["u2"], ids["u2"], ids["u3"]):  # a duplicate mid-loop must not poison the rest
        results.append(await svc.apply_do_not_sell(adal, adal.dal, user_id=user))
    assert results == [True, False, True]
    assert _opted_out(adal, ids["u2"]) and _opted_out(adal, ids["u3"])
