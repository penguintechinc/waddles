"""The CI minimal-FK bootstrap replays to the single head on real Postgres (no drift, no stubs).

CI's `Alembic migration chain (real Postgres)` job bootstraps only the handful of legacy
tables migrations 0020+ FK against, stamps 0019 and runs `alembic upgrade head`. The
`pg_docker._BOOTSTRAP_SQL` fixture grew extra columns/tables over time (a stub
`community_leaderboard_config`, `app_catalog.status`, `hub_users.display_name`, ...) that
masked migrations 0092/0095/0101 failing on the REAL CI bootstrap. This test extracts the
bootstrap SQL straight from the workflow file -- so it can never drift from CI -- and proves:

  * `alembic upgrade head` from that bootstrap succeeds and lands on exactly one head;
  * the objects 0092/0095 are responsible for really exist afterwards;
  * a downgrade to just before 0092 and a re-upgrade round-trips cleanly.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml
from pg_docker import DOCKER_AVAILABLE, REPO_ROOT, alembic_cli, empty_postgres

_WORKFLOW = Path(REPO_ROOT) / ".github" / "workflows" / "pr-validation.yml"
_STAMP = "0019_kick_app"
_BEFORE_0092 = "0091_marketplace_vendor_view"


def _ci_bootstrap_sql() -> str:
    """The exact SQL heredoc of the workflow's 'Bootstrap minimal FK schema' step."""
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["alembic-migration-chain"]["steps"]
    matches = [
        s for s in steps if s.get("name", "").startswith("Bootstrap minimal FK schema")
    ]
    assert len(matches) == 1, "the CI bootstrap step must exist exactly once"
    found = re.search(r"<<'SQL'\n(.*?)\nSQL", matches[0]["run"], re.DOTALL)
    assert found is not None, "CI bootstrap step lost its <<'SQL' heredoc"
    sql = found.group(1)
    assert (
        "CREATE TABLE tenants" in sql
    )  # zero-denominator guard: we parsed the real thing
    return sql


def _psql(container: str, user: str, dbname: str, sql: str) -> str:
    """Run `sql` in the container's psql, raising on any error; returns stdout."""
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "psql",
            "-U",
            user,
            "-d",
            dbname,
            "-v",
            "ON_ERROR_STOP=1",
            "-At",
        ],
        input=sql,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


@pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)
def test_ci_minimal_bootstrap_replays_to_single_head() -> None:
    """Upgrade from the CI bootstrap reaches the one head and creates the 0092/0095 objects."""
    with empty_postgres("ci-bootstrap-replay") as db:
        _psql(db.container, db.user, db.dbname, _ci_bootstrap_sql())
        alembic_cli("stamp", _STAMP, dsn=db.dsn)
        alembic_cli("upgrade", "head", dsn=db.dsn)

        heads = alembic_cli("heads", dsn=db.dsn).stdout.strip().splitlines()
        assert len(heads) == 1, f"expected exactly one alembic head, got {heads}"
        head_id = heads[0].split()[0]
        current = alembic_cli("current", dsn=db.dsn).stdout
        assert head_id in current, f"DB not at head {head_id}: {current!r}"

        cols = _psql(
            db.container,
            db.user,
            db.dbname,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'community_leaderboard_config' ORDER BY 1",
        ).split()
        for expected in (
            "watch_time_enabled",
            "messages_enabled",
            "public_leaderboard",
            "community_id",
        ):
            assert expected in cols, (
                f"0092 left community_leaderboard_config without {expected}: {cols}"
            )
        view = _psql(
            db.container,
            db.user,
            db.dbname,
            "SELECT count(*) FROM bundle_disabled_apps",
        ).strip()
        assert view == "0"  # the 0095 view is queryable (and empty on a fresh catalog)

        alembic_cli("downgrade", _BEFORE_0092, dsn=db.dsn)
        assert _BEFORE_0092 in alembic_cli("current", dsn=db.dsn).stdout
        alembic_cli("upgrade", "head", dsn=db.dsn)
        assert head_id in alembic_cli("current", dsn=db.dsn).stdout


@pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)
def test_0092_0095_are_noops_when_legacy_objects_already_exist() -> None:
    """Existing DBs (alpha, full legacy replay) already have the table/column: no schema change."""
    with empty_postgres("ci-bootstrap-existing") as db:
        _psql(db.container, db.user, db.dbname, _ci_bootstrap_sql())
        # Stage the legacy shapes exactly as config/postgres/migrations 004 / 069 create them.
        _psql(
            db.container,
            db.user,
            db.dbname,
            """
            CREATE TABLE community_leaderboard_config (
                id SERIAL PRIMARY KEY,
                community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE UNIQUE,
                enabled_platforms JSONB DEFAULT '[]',
                min_watch_time_minutes INTEGER DEFAULT 60,
                min_message_count INTEGER DEFAULT 10,
                display_limit INTEGER DEFAULT 10,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            ALTER TABLE app_catalog ADD COLUMN status TEXT DEFAULT 'disabled-sentinel';
            INSERT INTO app_catalog (app_id) VALUES ('waddles.t.f.a');
            """,
        )
        before = _psql(
            db.container,
            db.user,
            db.dbname,
            "SELECT column_name, column_default FROM information_schema.columns "
            "WHERE table_name = 'app_catalog' AND column_name = 'status'",
        )
        alembic_cli("stamp", _STAMP, dsn=db.dsn)
        alembic_cli("upgrade", "head", dsn=db.dsn)
        after = _psql(
            db.container,
            db.user,
            db.dbname,
            "SELECT column_name, column_default FROM information_schema.columns "
            "WHERE table_name = 'app_catalog' AND column_name = 'status'",
        )
        assert before == after, (
            "0095 must not alter an existing app_catalog.status column"
        )
        # The pre-existing row kept its default untouched by the migrations.
        assert (
            _psql(
                db.container, db.user, db.dbname, "SELECT status FROM app_catalog"
            ).strip()
            == "disabled-sentinel"
        )
