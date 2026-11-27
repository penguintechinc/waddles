r"""Seed SQL loads on the post-0148 schema and links members to ROLE ROWS (real Postgres + psql).

`config/postgres/seed_admin.sql` (run by `scripts/seed-admin.sh`) and the community/member block
of `config/postgres/seed_ui_mock.sql` (`scripts/seed-ui-mock-data.sh`) used to write the dropped
`community_members.role` string and `communities.owner_name`. They now seed the community's system
roles through the database's own `seed_community_system_roles()` and write `community_role_id`.
The seed files are executed by the REAL `psql` inside the migrated container (the `\\getenv`,
`\\set ON_ERROR_STOP` meta-commands need it), then the REAL authz resolver is asked what the
seeded admin/owner hold.

The minimal alembic bootstrap lacks a few legacy tables/unique keys the seeds' `ON CONFLICT`
clauses name; the fixture adds exactly those (never a role/owner column). seed_ui_mock.sql's
other feature tables (chat, overlays, leaderboards, ...) are outside the minimal schema, so its
community + member statements are executed verbatim, extracted from the file.

Skipped (never failed) where the `docker` CLI is unavailable, like every other PG test here.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import psycopg2
import pytest
from flask_core.database import AsyncDAL

from services import community_authz
from services.member_roles import COMMUNITY_ADMIN_ROLE, COMMUNITY_OWNER_ROLE
from tests import demo_pg_support as pg

pytestmark = pytest.mark.skipif(
    not pg.DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_SEEDS = Path(__file__).resolve().parents[2] / "config" / "postgres"
_ADMIN_EMAIL = "seed-admin@example.invalid"

#: Legacy objects the seeds' ON CONFLICT targets need that the minimal bootstrap omits.
_SEED_PREREQS = """
CREATE TABLE IF NOT EXISTS tenant_admins (
    id SERIAL PRIMARY KEY, tenant_id INTEGER, user_id INTEGER, role VARCHAR(50),
    UNIQUE (tenant_id, user_id));
CREATE TABLE IF NOT EXISTS tenant_members (
    id SERIAL PRIMARY KEY, tenant_id INTEGER, user_id INTEGER, UNIQUE (tenant_id, user_id));
CREATE UNIQUE INDEX IF NOT EXISTS ux_seed_hub_users_email ON hub_users (email);
CREATE UNIQUE INDEX IF NOT EXISTS ux_seed_communities_name ON communities (name);
CREATE UNIQUE INDEX IF NOT EXISTS ux_seed_cm_user ON community_members (community_id, user_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_seed_cm_platform
    ON community_members (community_id, platform, platform_user_id);
DO $$ BEGIN
    CREATE TYPE community_type AS ENUM ('creator', 'gaming', 'shared_interest_group');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
ALTER TABLE communities ALTER COLUMN community_type TYPE community_type
    USING community_type::community_type;
ALTER TABLE communities ADD COLUMN IF NOT EXISTS primary_platform VARCHAR(50);
ALTER TABLE communities ADD COLUMN IF NOT EXISTS is_global BOOLEAN DEFAULT FALSE;
ALTER TABLE community_members ADD COLUMN IF NOT EXISTS reputation INTEGER;
"""


@pytest.fixture(scope="module")
def pg_db() -> Iterator[pg.PgTestDatabase]:
    """One migrated Postgres container (post-0148) + real role seeding objects + seed prereqs."""
    with pg.migrated_postgres("hub-api-seed-sql") as db:
        pg.prepare_database(db)
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(_SEED_PREREQS)
        conn.close()
        yield db


@pytest.fixture
def adal(pg_db: pg.PgTestDatabase) -> Iterator[AsyncDAL]:
    """Per-test truncated, bound pydal `AsyncDAL` on the real container."""
    yield from pg.fresh_dal(pg_db)


def _psql(db: pg.PgTestDatabase, sql: str, env: dict[str, str] | None = None) -> None:
    """Run ``sql`` through the container's real psql (ON_ERROR_STOP); raise on any error."""
    cmd = ["docker", "exec", "-i"]
    for key in env or {}:
        cmd += ["-e", key]  # value inherited from the child env, never on the command line
    cmd += [db.container, "psql", "-v", "ON_ERROR_STOP=1", "-U", db.user, "-d", db.dbname]
    result = subprocess.run(  # noqa: S603 - fixed argv, test-only
        cmd,
        input=sql,
        text=True,
        capture_output=True,
        env={**os.environ, **(env or {})},
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr


def _columns(dal: object, table: str) -> set[str]:
    rows = pg.q(
        dal,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        [table],
    )
    return {r[0] for r in rows}


async def _linked_role(adal: AsyncDAL, community_id: int, user_id: int) -> tuple[str, bool]:
    """The (name, is_system) of the role row the member LINKS."""
    rows = pg.q(
        adal.dal,
        "SELECT r.name, r.is_system FROM community_members m "
        "JOIN community_roles r ON r.id = m.community_role_id "
        "WHERE m.community_id = %s AND m.user_id = %s",
        [community_id, str(user_id)],
    )
    assert rows, "member has no linked role"
    return rows[0][0], bool(rows[0][1])


async def test_schema_has_no_legacy_authority_columns(adal: AsyncDAL) -> None:
    """Post-0148 the string role and the free-form owner columns are gone."""
    assert "role" not in _columns(adal.dal, "community_members")
    assert "community_role_id" in _columns(adal.dal, "community_members")
    assert {"owner_id", "owner_name"}.isdisjoint(_columns(adal.dal, "communities"))


async def test_seed_admin_links_global_admin_role(adal: AsyncDAL, pg_db: pg.PgTestDatabase) -> None:
    """seed_admin.sql loads twice (idempotent) and links the admin to the system admin role."""
    env = {"ADMIN_EMAIL": _ADMIN_EMAIL, "ADMIN_PASSWORD": "seed-test-only-pass"}  # noqa: S105
    sql = (_SEEDS / "seed_admin.sql").read_text("utf-8")
    _psql(pg_db, sql, env)
    _psql(pg_db, sql, env)
    cid = pg.scalar(adal.dal, "SELECT id FROM communities WHERE name = 'waddlebot-global'")
    uid = pg.scalar(adal.dal, "SELECT id FROM hub_users WHERE email = %s", [_ADMIN_EMAIL])
    assert await _linked_role(adal, int(cid), int(uid)) == (COMMUNITY_ADMIN_ROLE, True)
    scopes = community_authz.member_scopes_sync(adal.dal, community_id=int(cid), user_id=int(uid))
    assert "community:manage_members" in scopes


def _ui_mock_community_block() -> str:
    """seed_ui_mock.sql's hub_users + communities + member statements, verbatim."""
    text = (_SEEDS / "seed_ui_mock.sql").read_text("utf-8")
    start = text.index("-- Demo hub users")
    end = text.index("-- Leaderboard:")
    block = text[start:end]
    assert "owner_name" not in block and not re.search(r"\brole\b\s*,", block)
    return "\\set ON_ERROR_STOP on\nBEGIN;\n" + block + "COMMIT;\n"


async def test_seed_ui_mock_links_owner_and_moderator(
    adal: AsyncDAL, pg_db: pg.PgTestDatabase
) -> None:
    """The mock communities seed their roles; 9001 owns every one, 9002 moderates 9001."""
    pg.q(
        adal.dal,
        "INSERT INTO tenants (slug, display_name, is_global, is_active) "
        "VALUES ('global', 'Global', true, true)",
    )
    block = _ui_mock_community_block()
    _psql(pg_db, block)
    _psql(pg_db, block)  # idempotent refresh
    for cid in (9001, 9002, 9003, 9004):
        assert await _linked_role(adal, cid, 9001) == (COMMUNITY_OWNER_ROLE, True)
        owner = await community_authz.community_owner_member(adal, adal.dal, community_id=cid)
        assert owner is not None and str(owner.user_id) == "9001"
    assert await _linked_role(adal, 9001, 9002) == ("moderator", True)
    assert await _linked_role(adal, 9001, 9005) == ("member", True)
    unlinked = pg.scalar(
        adal.dal,
        "SELECT count(*) FROM community_members "
        "WHERE community_id BETWEEN 9001 AND 9004 AND community_role_id IS NULL",
    )
    assert unlinked == 0
