"""MFA attempt cap on REAL Postgres: concurrent wrong codes never exceed ``MAX_ATTEMPTS``.

The old read-then-write counter let every racer read ``attempts = 0`` and each evaluate a guess,
so N parallel requests got N guesses at one challenge. The fix reserves the attempt FIRST with
``UPDATE ... SET attempts = attempts + 1 WHERE id = ? AND attempts < MAX AND consumed_at IS
NULL`` -- Postgres serialises those row updates, so at most ``MAX_ATTEMPTS`` codes are ever
checked. Real alembic-migrated schema (0145 + 0146), a real multi-connection pydal `AsyncDAL`,
`asyncio.gather` racers. Skipped where the `docker` CLI is unavailable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from flask_core.database import AsyncDAL
from pydal import Field
from quart import Quart

from services import dsar_rate_limit, mfa_service, totp
from services.errors import ApiError
from services.schema import bind_auth_tables
from tests import demo_pg_support as pg

pytestmark = pytest.mark.skipif(not pg.DOCKER_AVAILABLE, reason="docker CLI not available")

RACERS = 15  # below the per-account short-window budget (20): only the attempt cap limits them
TOKEN = "race-mfa-pending-token"  # noqa: S105 -- opaque test value


@pytest.fixture(scope="module")
def pg_db() -> Iterator[pg.PgTestDatabase]:
    """One migrated Postgres container for the module."""
    with pg.migrated_postgres("mfa-attempt-race") as db:
        pg.prepare_database(db)
        yield db


@pytest.fixture
def adal(pg_db: pg.PgTestDatabase) -> Iterator[AsyncDAL]:
    """A pooled AsyncDAL: one OS thread + connection per racer, like production."""
    async_dal = AsyncDAL(pg_db.dsn.replace("postgresql://", "postgres://", 1), pool_size=RACERS)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug"),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean"),
        Field("is_active", "boolean"),
        Field("config", "json"),
        migrate=False,
    )
    bind_auth_tables(dal, migrate=False)
    dal.executesql(
        "TRUNCATE hub_mfa_challenges, hub_user_mfa, hub_user_mfa_recovery_codes, hub_users "
        "RESTART IDENTITY CASCADE"
    )
    dal.commit()
    dsar_rate_limit.reset_local()
    yield async_dal
    dsar_rate_limit.reset_local()
    dal.close()


def _seed(dal: Any) -> tuple[int, int]:
    """One user with an ACTIVE factor and one live login challenge; returns (user, challenge)."""
    now = datetime.now(UTC)
    user_id = int(
        pg.scalar(
            dal,
            "INSERT INTO hub_users (username, email, email_verified, is_active) "
            "VALUES ('racer', NULL, false, true) RETURNING id",
        )
    )
    pg.q(
        dal,
        "INSERT INTO hub_user_mfa (user_id, totp_status, email_status) VALUES (%s, 2, 0)",
        [user_id],
    )
    challenge_id = int(
        pg.scalar(
            dal,
            "INSERT INTO hub_mfa_challenges (user_id, purpose, token_hash, attempts, email_sends, "
            "expires_at) VALUES (%s, 1, %s, 0, 0, %s) RETURNING id",
            [user_id, totp.hash_token(TOKEN), now + timedelta(minutes=10)],
        )
    )
    return user_id, challenge_id


async def test_concurrent_wrong_codes_never_exceed_the_attempt_cap(
    adal: AsyncDAL, monkeypatch: pytest.MonkeyPatch
) -> None:
    dal = adal.dal
    _user_id, challenge_id = _seed(dal)
    evaluated = 0

    async def slow_wrong_code(*_args: Any, **_kwargs: Any) -> bool:
        """The code check itself: always wrong, slow enough that every racer overlaps."""
        nonlocal evaluated
        evaluated += 1
        await asyncio.sleep(0.2)
        return False

    monkeypatch.setattr(mfa_service, "_consume_recovery_code", slow_wrong_code)

    async def attempt() -> BaseException | None:
        try:
            await mfa_service.complete_login(
                adal, dal, None, token=TOKEN, method="recovery", code="AAAAA-AAAAA"
            )
        except ApiError as exc:
            return exc
        return None

    async with Quart(__name__).app_context():
        results = await asyncio.gather(*[attempt() for _ in range(RACERS)])

    assert all(isinstance(r, ApiError) for r in results), results  # nobody got in
    assert 0 < evaluated <= mfa_service.MAX_ATTEMPTS, evaluated
    attempts, consumed = pg.q(
        dal, "SELECT attempts, consumed_at FROM hub_mfa_challenges WHERE id = %s", [challenge_id]
    )[0]
    assert attempts == mfa_service.MAX_ATTEMPTS
    assert consumed is not None  # burned at the cap
    codes = {getattr(r, "code", None) for r in results}
    assert codes <= {"MFA_INVALID_CODE", "MFA_CHALLENGE_INVALID", "MFA_RATE_LIMITED"}, codes
