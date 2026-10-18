"""Tests for scripts/demo/seed_bc_demo.py against a real Postgres 17 container.

DDL below mirrors the real columns/constraints of 0034/0036/0039 + legacy SQL 000/058
(only the parts the seed touches). Skipped only when docker is unavailable, matching
`alembic/tests/pg_docker.py`'s convention.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


seed_mod = _load("seed_bc_demo", "scripts/demo/seed_bc_demo.py")
pg = _load("pg_docker_demo", "alembic/tests/pg_docker.py")

pytestmark = pytest.mark.skipif(not pg.DOCKER_AVAILABLE, reason="docker not available")

DDL = """
CREATE TABLE communities (id SERIAL PRIMARY KEY, name TEXT);
CREATE TABLE hub_users (
    id SERIAL PRIMARY KEY, display_name VARCHAR(255), username VARCHAR(255) UNIQUE,
    is_active BOOLEAN DEFAULT true, uuid UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE);
CREATE TABLE hub_user_identities (
    id SERIAL PRIMARY KEY, hub_user_id INTEGER NOT NULL REFERENCES hub_users(id),
    platform VARCHAR(50) NOT NULL, platform_user_id VARCHAR(255) NOT NULL,
    is_primary BOOLEAN DEFAULT false,
    UNIQUE(hub_user_id, platform), UNIQUE(platform, platform_user_id));
CREATE TABLE community_roles (
    id SERIAL PRIMARY KEY, community_id INTEGER NOT NULL REFERENCES communities(id),
    name VARCHAR(50) NOT NULL, UNIQUE(community_id, name));
CREATE TABLE community_members (
    id SERIAL PRIMARY KEY, community_id INTEGER REFERENCES communities(id),
    user_id VARCHAR(255), platform VARCHAR(50), platform_user_id VARCHAR(255),
    role VARCHAR(50) DEFAULT 'member', community_role_id INTEGER REFERENCES community_roles(id),
    claims_cache JSONB, is_active BOOLEAN DEFAULT true, left_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(community_id, platform, platform_user_id));
CREATE TABLE guild_tenant_pairings (
    id BIGSERIAL PRIMARY KEY, community_id INTEGER NOT NULL REFERENCES communities(id),
    discord_guild_id VARCHAR(255) NOT NULL,
    direction VARCHAR(20) NOT NULL
        CHECK (direction IN ('discord_to_twitch','twitch_to_discord','bidirectional')),
    sync_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    event_sync_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    role_name_prefix VARCHAR(50) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (community_id, discord_guild_id));
CREATE TABLE community_role_sync_bindings (
    id BIGSERIAL PRIMARY KEY,
    pairing_id BIGINT NOT NULL REFERENCES guild_tenant_pairings(id) ON DELETE CASCADE,
    sync_scope VARCHAR(20) NOT NULL
        CHECK (sync_scope IN ('subscriber_tier','moderator','community_role')),
    subscriber_tier SMALLINT, discord_role_id VARCHAR(255) NOT NULL,
    community_role VARCHAR(20)
        CHECK (community_role IS NULL OR community_role IN
               ('community-admin','moderator','vip','member')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE UNIQUE INDEX uq_role_sync_binding_community_role
    ON community_role_sync_bindings (pairing_id, discord_role_id)
    WHERE sync_scope = 'community_role';
INSERT INTO communities (id, name) VALUES (1, 'waddlebot');
INSERT INTO community_roles (community_id, name) VALUES
    (1, 'member'), (1, 'moderator'), (1, 'community-owner');
"""


@pytest.fixture(scope="module")
def dsn() -> Iterator[str]:
    """One throwaway Postgres container with the seed-relevant schema loaded."""
    with pg.empty_postgres("bcdemo") as db:
        conn = psycopg2.connect(db.dsn)
        with conn, conn.cursor() as cur:
            cur.execute(DDL)
        conn.close()
        yield db.dsn


@pytest.fixture()
def conn(dsn: str) -> Iterator[psycopg2.extensions.connection]:
    """Fresh connection per test; seed tables truncated first (communities/roles kept)."""
    c = psycopg2.connect(dsn)
    with c, c.cursor() as cur:
        cur.execute(
            "TRUNCATE community_role_sync_bindings, guild_tenant_pairings, community_members, "
            "hub_user_identities, hub_users RESTART IDENTITY CASCADE"
        )
    yield c
    c.close()


def _count(conn: psycopg2.extensions.connection, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 -- fixed test table names
        return int(cur.fetchone()[0])


def test_seed_creates_all_links(conn: psycopg2.extensions.connection) -> None:
    cfg = seed_mod.load_config({})
    result = seed_mod.seed(conn, cfg)
    assert result.binding_count == 1
    with conn.cursor() as cur:
        cur.execute("SELECT direction, sync_enabled, event_sync_enabled FROM guild_tenant_pairings")
        assert cur.fetchall() == [("discord_to_twitch", True, True)]
        cur.execute("SELECT role, user_id, is_active FROM community_members")
        assert cur.fetchall() == [("member", str(result.hub_user_id), True)]
        cur.execute("SELECT sync_scope, community_role FROM community_role_sync_bindings")
        assert cur.fetchall() == [("community_role", "moderator")]
    assert _count(conn, "hub_user_identities") == 1


def test_seed_is_idempotent(conn: psycopg2.extensions.connection) -> None:
    cfg = seed_mod.load_config({})
    first = seed_mod.seed(conn, cfg)
    second = seed_mod.seed(conn, cfg)
    assert first == second
    for table in (
        "hub_users", "hub_user_identities", "community_members",
        "guild_tenant_pairings", "community_role_sync_bindings",
    ):
        assert _count(conn, table) == 1, table


def test_reseed_resets_promoted_role_but_never_owner(conn: psycopg2.extensions.connection) -> None:
    cfg = seed_mod.load_config({})
    seed_mod.seed(conn, cfg)
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE community_members SET role = 'moderator'")
    seed_mod.seed(conn, cfg)
    with conn.cursor() as cur:
        cur.execute("SELECT role FROM community_members")
        assert cur.fetchone()[0] == "member"
        cur.execute("UPDATE community_members SET role = 'community-owner'")
    conn.commit()
    seed_mod.seed(conn, cfg)
    with conn.cursor() as cur:
        cur.execute("SELECT role FROM community_members")
        assert cur.fetchone()[0] == "community-owner"


def test_binding_swap_updates_in_place(conn: psycopg2.extensions.connection) -> None:
    seed_mod.seed(conn, seed_mod.load_config({}))
    cfg = seed_mod.load_config({"DEMO_COMMUNITY_ROLE": "vip"})
    seed_mod.seed(conn, cfg)
    with conn.cursor() as cur:
        cur.execute("SELECT community_role FROM community_role_sync_bindings")
        assert cur.fetchall() == [("vip",)]


def test_missing_table_fails_loud_and_writes_nothing(
    conn: psycopg2.extensions.connection,
) -> None:
    with conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE community_role_sync_bindings RENAME TO crsb_gone")
    try:
        with pytest.raises(seed_mod.SeedError, match="community_role_sync_bindings"):
            seed_mod.seed(conn, seed_mod.load_config({}))
    finally:
        with conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE crsb_gone RENAME TO community_role_sync_bindings")
    assert _count(conn, "hub_users") == 0


def test_missing_column_fails_loud(conn: psycopg2.extensions.connection) -> None:
    with conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE guild_tenant_pairings RENAME COLUMN event_sync_enabled TO x")
    try:
        with pytest.raises(seed_mod.SeedError, match="guild_tenant_pairings.event_sync_enabled"):
            seed_mod.seed(conn, seed_mod.load_config({}))
    finally:
        with conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE guild_tenant_pairings RENAME COLUMN x TO event_sync_enabled")


def test_missing_community_and_baseline_fail_loud(conn: psycopg2.extensions.connection) -> None:
    with pytest.raises(seed_mod.SeedError, match="does not exist"):
        seed_mod.seed(conn, seed_mod.load_config({"DEMO_COMMUNITY_ID": "99"}))
    with pytest.raises(seed_mod.SeedError, match="baseline role"):
        seed_mod.seed(conn, seed_mod.load_config({"DEMO_BASELINE_ROLE": "vip"}))


@pytest.mark.parametrize(
    "env",
    [
        {"DEMO_COMMUNITY_ROLE": "community-owner"},
        {"DEMO_DIRECTION": "sideways"},
        {"DEMO_GUILD_ID": "abc"},
        {"DEMO_COMMUNITY_ID": "0"},
        {"DEMO_BASELINE_ROLE": "moderator"},
    ],
)
def test_invalid_config_rejected(env: dict[str, str]) -> None:
    with pytest.raises(seed_mod.SeedError):
        seed_mod.load_config(env)


def test_logs_are_pii_free(conn: psycopg2.extensions.connection, caplog: pytest.LogCaptureFixture) -> None:
    cfg = seed_mod.load_config({})
    with caplog.at_level("DEBUG", logger="seed_bc_demo"):
        seed_mod.seed(conn, cfg)
    text = caplog.text
    assert cfg.discord_user_id not in text
    assert "BC Demo Member" not in text
    assert "seed_bc_demo.binding_ok" in text


def test_main_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert seed_mod.main() == 2
