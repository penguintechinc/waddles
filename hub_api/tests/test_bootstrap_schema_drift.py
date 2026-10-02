"""Real-Postgres drift check for bootstrap.py's create_all()+stamp path.

Does it reproduce the same schema as running the full Alembic migration
chain on a fresh database?

fix/chart-fresh-install-hooks (alpha 2026-10-01) -- per the design decision moving
fresh-install schema creation into hub-api's own startup path (bootstrap.py), this test
was explicitly requested to catch drift between the two paths. It reuses
`alembic/tests/pg_docker.py`'s existing real-Postgres-container harness: `migrated_postgres()`
(already used by `alembic/tests/test_0028_*`) for the full-chain reference, and the new
`empty_postgres()` for a bare container bootstrap.py's `create_all()` runs against.

RESULT (documented, not silently hidden): `flask_core.models.db.metadata` only declares
~14 tables (auth_role/auth_user/communities/hub_users/video_*/engagement_* --
libs/flask_core/flask_core/models/). The migration chain's baseline replay
(alembic/versions/0001_baseline_from_sql_migrations.py) plus ~30 further revisions create
well over 100 tables via raw SQL with no corresponding SQLAlchemy model (migrations/
run-alembic.sh's own required-table check names `commands`/`platform_integrations`
explicitly). `create_all()` therefore does NOT reproduce the full migration-chain schema
on a truly empty database -- confirmed here, not fixed here (porting 100+ tables of raw
SQL into SQLAlchemy models is out of this fix's scope/budget; reported in the PR
description for a follow-up decision). `xfail(strict=True)`: if this ever starts passing
unexpectedly (e.g. someone backfills the missing models), that's a signal to deliberately
re-evaluate and drop the marker, not a silently-accepted drift.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import sqlalchemy

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "alembic" / "tests"))
sys.path.insert(0, str(REPO_ROOT / "hub_api"))

from pg_docker import DOCKER_AVAILABLE, empty_postgres, migrated_postgres  # noqa: E402

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)


def _table_names(dsn: str) -> set[str]:
    engine = sqlalchemy.create_engine(dsn.replace("postgresql://", "postgresql+psycopg2://", 1))
    try:
        return set(sqlalchemy.inspect(engine).get_table_names(schema="public"))
    finally:
        engine.dispose()


@requires_docker
@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN GAP (fix/chart-fresh-install-hooks, 2026-10-01): flask_core.models.db."
        "metadata only covers ~14 tables; the full migration chain creates 100+. See "
        "this module's docstring and bootstrap.py's _sync_bootstrap_attempt comment."
    ),
)
def test_create_all_matches_full_migration_chain_schema() -> None:
    """Assert create_all() produces the same table set as the full migration chain.

    Currently does not (see module docstring).
    """
    from flask_core.models import db as models_db  # noqa: PLC0415

    from bootstrap import _script_heads, _stamp_heads  # noqa: PLC0415

    with (
        migrated_postgres("drift-reference") as reference_db,
        empty_postgres("drift-createall") as createall_db,
    ):
        reference_tables = _table_names(reference_db.dsn)

        engine = sqlalchemy.create_engine(
            createall_db.dsn.replace("postgresql://", "postgresql+psycopg2://", 1)
        )
        try:
            heads = _script_heads(str(REPO_ROOT / "alembic"))
            with engine.begin() as conn:
                models_db.metadata.create_all(bind=conn)
                _stamp_heads(conn, heads)
        finally:
            engine.dispose()
        createall_tables = _table_names(createall_db.dsn)

        missing = reference_tables - createall_tables
        assert not missing, (
            f"create_all() is missing {len(missing)} table(s) the full migration chain "
            f"creates: {sorted(missing)[:10]}{'...' if len(missing) > 10 else ''}"
        )
