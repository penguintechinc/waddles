"""Real-Postgres tests for 0133_precompile_stale_reclaims (poison-pill guard).

Runs the service's OWN SQL (read verbatim from hub_api/services/bundle_precompile_service.py)
against a migrated database: the column defaults to 0, expired-lease reclaims count up, the row
past the cap is dead-lettered with the documented error (and never claimed again), a revive resets
both counters, and the migration downgrades/re-upgrades cleanly. A SKIP means docker is absent --
treat as FAILURE.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

PREVIOUS_REVISION = "0132_community_public_id_read"
_SERVICE = (
    Path(__file__).resolve().parents[2] / "hub_api" / "services" / "bundle_precompile_service.py"
)
DIGEST = "cd" * 32
CAP = 3


def _service_sql() -> dict[str, str]:
    """Module-level string constants of the service (no hub_api imports needed)."""
    out: dict[str, str] = {}
    for node in ast.parse(_SERVICE.read_text()).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            out[node.targets[0].id] = node.value.value
    return out


def _engine(db: PgTestDatabase) -> sqlalchemy.Engine:
    return sqlalchemy.create_engine(db.dsn.replace("postgresql://", "postgresql+psycopg2://", 1))


@pytest.fixture(scope="module")
def db() -> Iterator[PgTestDatabase]:
    """One real Postgres container migrated to head (includes 0133)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0133-stale") as database:
        yield database


@requires_docker
def test_stale_reclaims_count_then_dead_letter_then_revive(db: PgTestDatabase) -> None:
    sql = _service_sql()
    engine = _engine(db)
    t = sqlalchemy.text
    expire = t("UPDATE bundle_precompile_requests SET claimed_at = NOW() - INTERVAL '1 hour'")
    state = t("SELECT attempts, stale_reclaims, last_error FROM bundle_precompile_requests")
    claim = {"limit": 10, "lease": 60, "max_attempts": 5}
    stale = {"lease": 60, "max_attempts": 5, "max_stale_reclaims": CAP}
    with engine.begin() as conn:
        conn.execute(
            t(sql["_ENQUEUE_SQL"]), {"digest": DIGEST, "app_id": "waddles.test", "version": "1"}
        )
        assert tuple(conn.execute(state).one()) == (0, 0, None)  # column defaults to 0

        # First claim is not a reclaim; each expired-lease reclaim up to the cap is re-offered.
        assert len(conn.execute(t(sql["_CLAIM_SQL"]), claim).fetchall()) == 1
        assert conn.execute(state).one().stale_reclaims == 0
        for expected in range(1, CAP + 1):
            conn.execute(expire)
            assert conn.execute(t(sql["_STALE_DEAD_LETTER_SQL"]), stale).fetchall() == []
            assert len(conn.execute(t(sql["_CLAIM_SQL"]), claim).fetchall()) == 1
            row = conn.execute(state).one()
            assert (row.attempts, row.stale_reclaims) == (0, expected)

        # One more expiry exceeds the cap: dead-lettered with the documented error, not offered.
        conn.execute(expire)
        (hit,) = conn.execute(t(sql["_STALE_DEAD_LETTER_SQL"]), stale).fetchall()
        assert (hit.app_id, hit.stale_reclaims) == ("waddles.test", CAP + 1)
        assert conn.execute(t(sql["_CLAIM_SQL"]), claim).fetchall() == []
        row = conn.execute(state).one()
        assert row.attempts >= 5
        assert row.last_error == f"lease expired {CAP + 1} times without ack (runner crash/timeout)"
        assert conn.execute(t(sql["_DEAD_COUNT_SQL"]), {"max_attempts": 5}).scalar() == 1
        # Already dead: the guard does not fire (or re-count) again.
        conn.execute(expire)
        assert conn.execute(t(sql["_STALE_DEAD_LETTER_SQL"]), stale).fetchall() == []

        # Revive resets BOTH counters and clears the error.
        revive = {"digest": DIGEST, "max_attempts": 5}
        assert len(conn.execute(t(sql["_REVIVE_SQL"]), revive).fetchall()) == 1
        assert tuple(conn.execute(state).one()) == (0, 0, None)

        # A genuine failed ack resets the stale counter.
        conn.execute(t("UPDATE bundle_precompile_requests SET stale_reclaims = 2"))
        conn.execute(t(sql["_FAIL_SQL"]), {"digest": DIGEST, "error": "boom"})
        assert conn.execute(state).one().stale_reclaims == 0
    engine.dispose()


@requires_docker
def test_stale_reclaims_check_constraint(db: PgTestDatabase) -> None:
    engine = _engine(db)
    with pytest.raises(sqlalchemy.exc.IntegrityError), engine.begin() as conn:
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO bundle_precompile_requests "
                "(artifact_digest, app_id, version, stale_reclaims) VALUES ('neg', 'a', '1', -1)"
            )
        )
    engine.dispose()


@requires_docker
def test_downgrade_drops_column_and_reupgrade_restores() -> None:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    probe = (
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'bundle_precompile_requests' AND column_name = 'stale_reclaims'"
    )
    with migrated_postgres("0133-roundtrip") as database:
        engine = _engine(database)
        with engine.connect() as conn:
            assert conn.execute(sqlalchemy.text(probe)).scalar() == 1
        alembic_cli("downgrade", PREVIOUS_REVISION, dsn=database.dsn)
        with engine.connect() as conn:
            assert conn.execute(sqlalchemy.text(probe)).scalar() == 0
        alembic_cli("upgrade", "head", dsn=database.dsn)
        with engine.connect() as conn:
            assert conn.execute(sqlalchemy.text(probe)).scalar() == 1
        engine.dispose()
