"""Real-Postgres tests for 0101_bundle_precompile_requests (AOT precompile queue).

Proves the table lands, the UNIQUE(artifact_digest) makes enqueue idempotent, and that the
service's OWN lease SQL (read verbatim from hub_api/services/bundle_precompile_service.py) claims
a row once, honours the lease, and stops at the attempt cap. A SKIP means docker is absent --
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

PREVIOUS_REVISION = "0100_stream_dek_issuances"
_SERVICE = (
    Path(__file__).resolve().parents[2]
    / "hub_api"
    / "services"
    / "bundle_precompile_service.py"
)
DIGEST = "ab" * 32


def _service_sql() -> dict[str, str]:
    """Module-level string constants of the service (no hub_api imports needed)."""
    tree = ast.parse(_SERVICE.read_text())
    out: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            out[node.targets[0].id] = node.value.value
    return out


@pytest.fixture(scope="module")
def db() -> Iterator[PgTestDatabase]:
    """One real Postgres container migrated to head (includes 0101)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0101-precompile") as database:
        yield database


def _engine(db: PgTestDatabase) -> sqlalchemy.Engine:
    return sqlalchemy.create_engine(
        db.dsn.replace("postgresql://", "postgresql+psycopg2://", 1)
    )


@requires_docker
def test_enqueue_is_idempotent_and_lease_claims_once(db: PgTestDatabase) -> None:
    sql = _service_sql()
    engine = _engine(db)
    params = {"digest": DIGEST, "app_id": "waddles.test", "version": "1.0.0"}
    with engine.begin() as conn:
        first = conn.execute(sqlalchemy.text(sql["_ENQUEUE_SQL"]), params).fetchall()
        second = conn.execute(sqlalchemy.text(sql["_ENQUEUE_SQL"]), params).fetchall()
    assert (len(first), len(second)) == (1, 0)

    claim = {"limit": 10, "lease": 1800, "max_attempts": 2}
    with engine.begin() as conn:
        got = conn.execute(sqlalchemy.text(sql["_CLAIM_SQL"]), claim).fetchall()
        again = conn.execute(sqlalchemy.text(sql["_CLAIM_SQL"]), claim).fetchall()
    assert [r.artifact_digest for r in got] == [DIGEST]
    assert again == []  # leased

    # Expire the lease: claimable again; then the attempt cap (2) stops it.
    with engine.begin() as conn:
        conn.execute(
            sqlalchemy.text(
                "UPDATE bundle_precompile_requests SET claimed_at = NOW() - INTERVAL '1 hour'"
            )
        )
        assert (
            len(
                conn.execute(
                    sqlalchemy.text(sql["_CLAIM_SQL"]), {**claim, "lease": 60}
                ).fetchall()
            )
            == 1
        )
        conn.execute(
            sqlalchemy.text(
                "UPDATE bundle_precompile_requests SET claimed_at = NOW() - INTERVAL '1 hour'"
            )
        )
        assert (
            conn.execute(
                sqlalchemy.text(sql["_CLAIM_SQL"]), {**claim, "lease": 60}
            ).fetchall()
            == []
        )
        conn.execute(sqlalchemy.text(sql["_DELETE_SQL"]), {"digest": DIGEST})
        left = conn.execute(
            sqlalchemy.text("SELECT count(*) FROM bundle_precompile_requests")
        ).scalar()
    assert left == 0
    engine.dispose()


@requires_docker
def test_attempts_check_constraint(db: PgTestDatabase) -> None:
    engine = _engine(db)
    with pytest.raises(sqlalchemy.exc.IntegrityError), engine.begin() as conn:
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO bundle_precompile_requests (artifact_digest, app_id, version, attempts) "
                "VALUES ('x', 'a', '1', -1)"
            )
        )
    engine.dispose()


@requires_docker
def test_downgrade_drops_table_and_reupgrade_restores() -> None:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0101-roundtrip") as database:
        alembic_cli("downgrade", PREVIOUS_REVISION, dsn=database.dsn)
        engine = _engine(database)
        with engine.connect() as conn:
            assert (
                conn.execute(
                    sqlalchemy.text("SELECT to_regclass('bundle_precompile_requests')")
                ).scalar()
                is None
            )
        alembic_cli("upgrade", "head", dsn=database.dsn)
        with engine.connect() as conn:
            assert (
                conn.execute(
                    sqlalchemy.text("SELECT to_regclass('bundle_precompile_requests')")
                ).scalar()
                is not None
            )
        engine.dispose()
