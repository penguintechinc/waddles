"""Real-Postgres tests for 0084_user_active_context (per-user slash-command active community).

Proves the migration lands on a real Postgres 17 (`pg_docker.migrated_postgres`, which replays
0020 through head), round-trips upgrade -> downgrade -> upgrade, and enforces what the routing relies
on: one row per (user_uuid, platform, source_id), canonical platform/source CHECKs, FKs to
`tenants` / `communities` (cascade on community delete), and hub-api-only table privileges.
A SKIP means docker is absent -- treat it as a gate FAILURE, never a pass.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

#: The revision this migration chains off -- the round-trip downgrades to exactly it.
PREVIOUS_REVISION = "0083_eventsub_desired_state"

EXPECTED_COLUMNS = sorted(
    ["id", "tenant_id", "user_uuid", "platform", "source_id", "community_id", "updated_at"]
)


def _connect(db: PgTestDatabase) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    return conn


def _tenant_and_community(cur: psycopg2.extensions.cursor) -> tuple[int, int]:
    cur.execute(
        "INSERT INTO tenants (slug) VALUES (%s) RETURNING id", (f"t-{uuid.uuid4().hex[:8]}",)
    )
    tenant_id = int(cur.fetchone()[0])
    cur.execute(
        "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id",
        (f"c-{uuid.uuid4().hex[:8]}", tenant_id),
    )
    return tenant_id, int(cur.fetchone()[0])


def _insert(
    cur: psycopg2.extensions.cursor,
    tenant_id: int,
    community_id: int,
    *,
    user: str | None = None,
    platform: str = "discord",
    source_id: str = "474965105759748096",
) -> None:
    cur.execute(
        "INSERT INTO user_active_context "
        "(tenant_id, user_uuid, platform, source_id, community_id) VALUES (%s, %s, %s, %s, %s)",
        (tenant_id, user or str(uuid.uuid4()), platform, source_id, community_id),
    )


@pytest.fixture(scope="module")
def db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container migrated to head, shared by the non-round-trip tests."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0084-user-active-context") as database:
        yield database


@requires_docker
def test_upgrade_creates_columns_and_index(db: PgTestDatabase) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'user_active_context' ORDER BY column_name"
            )
            columns = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT count(*) FROM pg_indexes WHERE tablename = 'user_active_context' "
                "AND indexname = 'idx_user_active_context_community'"
            )
            assert cur.fetchone() == (1,)
    finally:
        conn.close()
    assert columns == EXPECTED_COLUMNS


@requires_docker
def test_unique_user_platform_source(db: PgTestDatabase) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            tid, cid = _tenant_and_community(cur)
            user = str(uuid.uuid4())
            _insert(cur, tid, cid, user=user)
            _insert(cur, tid, cid, user=user, source_id="other")  # other source: another row
        with pytest.raises(psycopg2.errors.UniqueViolation), conn.cursor() as cur:
            _insert(cur, tid, cid, user=user)
    finally:
        conn.close()


@requires_docker
@pytest.mark.parametrize(
    ("platform", "source_id"),
    [("Discord", "1"), ("1discord", "1"), ("disc ord", "1"), ("discord", "bad id"), ("discord", "")],
)
def test_checks_reject_noncanonical(db: PgTestDatabase, platform: str, source_id: str) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            tid, cid = _tenant_and_community(cur)
        with pytest.raises(psycopg2.errors.CheckViolation), conn.cursor() as cur:
            _insert(cur, tid, cid, platform=platform, source_id=source_id)
    finally:
        conn.close()


@requires_docker
def test_user_uuid_is_a_real_uuid_type(db: PgTestDatabase) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            tid, cid = _tenant_and_community(cur)
        with pytest.raises(psycopg2.errors.InvalidTextRepresentation), conn.cursor() as cur:
            _insert(cur, tid, cid, user="not-a-uuid")
    finally:
        conn.close()


@requires_docker
def test_fks_enforced_and_community_delete_cascades(db: PgTestDatabase) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            tid, cid = _tenant_and_community(cur)
        with pytest.raises(psycopg2.errors.ForeignKeyViolation), conn.cursor() as cur:
            _insert(cur, tid, 987654321)
        with pytest.raises(psycopg2.errors.ForeignKeyViolation), conn.cursor() as cur:
            _insert(cur, 987654321, cid)
        with conn.cursor() as cur:
            _insert(cur, tid, cid)
            cur.execute("DELETE FROM communities WHERE id = %s", (cid,))
            cur.execute("SELECT count(*) FROM user_active_context WHERE community_id = %s", (cid,))
            assert cur.fetchone() == (0,)
    finally:
        conn.close()


@requires_docker
def test_grants_hub_api_only(db: PgTestDatabase) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                cur.execute("SELECT has_table_privilege('hub_api', 'user_active_context', %s)", (priv,))
                assert cur.fetchone() == (True,), f"hub_api lacks {priv}"
            for role in (
                "svc_ingest",
                "svc_process",
                "svc_action",
                "svc_streaming",
                "webui",
                "waddles_publisher",
            ):
                cur.execute(
                    "SELECT has_table_privilege(%s, 'user_active_context', 'SELECT')", (role,)
                )
                assert cur.fetchone() == (False,), f"{role} can read user_active_context"
            cur.execute(
                "SELECT has_sequence_privilege('hub_api', 'user_active_context_id_seq', 'USAGE')"
            )
            assert cur.fetchone() == (True,)
    finally:
        conn.close()


@requires_docker
@pytest.mark.parametrize("table", ["tenants", "communities", "app_catalog"])
def test_public_id_columns_are_unique_not_null_uuid(db: PgTestDatabase, table: str) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = %s AND column_name = 'public_id'",
                (table,),
            )
            data_type, nullable, default = cur.fetchone()
            assert (data_type, nullable) == ("uuid", "NO")
            assert "gen_random_uuid" in default
            cur.execute(
                "SELECT count(*) FROM pg_indexes WHERE tablename = %s AND indexname = %s",
                (table, f"uq_{table}_public_id"),
            )
            assert cur.fetchone() == (1,)
    finally:
        conn.close()


@requires_docker
def test_public_ids_are_generated_distinct_and_unique(db: PgTestDatabase) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            tid, cid = _tenant_and_community(cur)
            tid2, cid2 = _tenant_and_community(cur)
            cur.execute("SELECT count(DISTINCT public_id) FROM tenants WHERE id IN (%s, %s)", (tid, tid2))
            assert cur.fetchone() == (2,)
            cur.execute("SELECT public_id FROM communities WHERE id = %s", (cid,))
            existing = cur.fetchone()[0]
        with pytest.raises(psycopg2.errors.UniqueViolation), conn.cursor() as cur:
            cur.execute("UPDATE communities SET public_id = %s WHERE id = %s", (existing, cid2))
    finally:
        conn.close()


@requires_docker
def test_round_trip_downgrade_upgrade() -> None:
    with migrated_postgres("0084-user-active-context-roundtrip") as database:
        alembic_cli("downgrade", PREVIOUS_REVISION, dsn=database.dsn)
        conn = _connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('user_active_context')")
                assert cur.fetchone() == (None,), "downgrade left the table behind"
                cur.execute(
                    "SELECT count(*) FROM information_schema.columns WHERE column_name = "
                    "'public_id' AND table_name IN ('tenants','communities','app_catalog')"
                )
                assert cur.fetchone() == (0,), "downgrade left public_id behind"
        finally:
            conn.close()
        alembic_cli("upgrade", "head", dsn=database.dsn)
        conn = _connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('user_active_context') IS NOT NULL")
                assert cur.fetchone() == (True,)
                cur.execute(
                    "SELECT count(*) FROM information_schema.columns WHERE column_name = "
                    "'public_id' AND table_name IN ('tenants','communities','app_catalog')"
                )
                assert cur.fetchone() == (3,)
        finally:
            conn.close()
