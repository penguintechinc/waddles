"""Real-Postgres tests for 0062_app_commands_registry (slash-command registry, Phase 1).

Proves the migration lands on a real Postgres 17 (`pg_docker.migrated_postgres`, which replays
0020 through head), round-trips upgrade -> downgrade -> upgrade, and enforces what the registry
relies on: a validated command token (or the ``*`` wildcard), a 1..3 ``command_types`` bitmask,
a non-empty ``platforms`` JSON array, one row per (tenant, community, app, command), an FK to
``app_catalog``, and hub-api-only table privileges.
"""

from __future__ import annotations

import json
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

#: The revision this migration chains off -- the round-trip downgrades to exactly it, never past it.
PREVIOUS_REVISION = "0061_port_legacy_sql_103_codec"

EXPECTED_COLUMNS = sorted(
    [
        "id",
        "tenant_id",
        "community_id",
        "app_id",
        "command",
        "command_types",
        "platforms",
        "description",
        "registered_at",
    ]
)


def _connect(db: PgTestDatabase) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    return conn


def _tenant(cur: psycopg2.extensions.cursor) -> int:
    cur.execute("INSERT INTO tenants (slug) VALUES (%s) RETURNING id", (f"t-{uuid.uuid4().hex[:8]}",))
    return int(cur.fetchone()[0])


def _app(cur: psycopg2.extensions.cursor) -> str:
    app_id = f"app-{uuid.uuid4().hex[:8]}"
    cur.execute("INSERT INTO app_catalog (app_id) VALUES (%s)", (app_id,))
    return app_id


def _insert(
    cur: psycopg2.extensions.cursor,
    tenant_id: int,
    app_id: str,
    *,
    command: str = "ping",
    command_types: int = 1,
    platforms: str = '["discord"]',
    community_id: int = 0,
) -> int:
    cur.execute(
        "INSERT INTO app_commands "
        "(tenant_id, community_id, app_id, command, command_types, platforms) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb) RETURNING id",
        (tenant_id, community_id, app_id, command, command_types, platforms),
    )
    return int(cur.fetchone()[0])


@pytest.fixture(scope="module")
def db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container migrated to head, shared by the non-round-trip tests."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0062-app-commands") as database:
        yield database


@requires_docker
def test_upgrade_creates_app_commands_columns(db: PgTestDatabase) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'app_commands' ORDER BY column_name"
            )
            columns = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'app_commands' AND indexname = 'idx_app_commands_owner'"
            )
            [(definition,)] = cur.fetchall()
    finally:
        conn.close()
    assert columns == EXPECTED_COLUMNS
    assert "(tenant_id, community_id, command, registered_at, id)" in definition


@requires_docker
def test_unique_tenant_community_app_command(db: PgTestDatabase) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            tid, app = _tenant(cur), _app(cur)
            _insert(cur, tid, app)
            _insert(cur, tid, app, community_id=5)  # different community: a different row
        with pytest.raises(psycopg2.errors.UniqueViolation), conn.cursor() as cur:
            _insert(cur, tid, app)
    finally:
        conn.close()


@requires_docker
def test_command_check_accepts_wildcard_and_verbs(db: PgTestDatabase) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            tid, app = _tenant(cur), _app(cur)
            for command in ("*", "ping", "chat-history"):
                _insert(cur, tid, app, command=command)
    finally:
        conn.close()


@requires_docker
@pytest.mark.parametrize("bad", ["Ping", "!ping", "a b", "", "x" * 33])
def test_command_check_rejects(db: PgTestDatabase, bad: str) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            tid, app = _tenant(cur), _app(cur)
        # A 33-char token cannot even be stored (VARCHAR(32) truncation fires before the CHECK);
        # every other bad token trips the CHECK. Either way it never lands in the table.
        with (
            pytest.raises((psycopg2.errors.CheckViolation, psycopg2.errors.StringDataRightTruncation)),
            conn.cursor() as cur,
        ):
            _insert(cur, tid, app, command=bad)
    finally:
        conn.close()


@requires_docker
@pytest.mark.parametrize("bad", [0, 4])
def test_command_types_check_rejects(db: PgTestDatabase, bad: int) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            tid, app = _tenant(cur), _app(cur)
            for good in (1, 2, 3):
                _insert(cur, tid, app, command=f"ok{good}", command_types=good)
        with pytest.raises(psycopg2.errors.CheckViolation), conn.cursor() as cur:
            _insert(cur, tid, app, command="bad", command_types=bad)
    finally:
        conn.close()


@requires_docker
def test_platforms_must_be_nonempty_array(db: PgTestDatabase) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            tid, app = _tenant(cur), _app(cur)
        for bad in ("[]", json.dumps("discord")):
            with pytest.raises(psycopg2.errors.CheckViolation), conn.cursor() as cur:
                _insert(cur, tid, app, platforms=bad)
    finally:
        conn.close()


@requires_docker
def test_app_id_fk_enforced(db: PgTestDatabase) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            tid = _tenant(cur)
        with pytest.raises(psycopg2.errors.ForeignKeyViolation), conn.cursor() as cur:
            _insert(cur, tid, "no-such-app")
    finally:
        conn.close()


@requires_docker
def test_grants_hub_api_only(db: PgTestDatabase) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                cur.execute("SELECT has_table_privilege('hub_api', 'app_commands', %s)", (priv,))
                assert cur.fetchone() == (True,), f"hub_api lacks {priv}"
            for role in (
                "svc_ingest",
                "svc_process",
                "svc_action",
                "svc_streaming",
                "webui",
                "waddles_publisher",
            ):
                cur.execute("SELECT has_table_privilege(%s, 'app_commands', 'SELECT')", (role,))
                assert cur.fetchone() == (False,), f"{role} can read app_commands"
            cur.execute("SELECT has_sequence_privilege('hub_api', 'app_commands_id_seq', 'USAGE')")
            assert cur.fetchone() == (True,)
    finally:
        conn.close()


@requires_docker
def test_round_trip_downgrade_upgrade() -> None:
    with migrated_postgres("0062-app-commands-roundtrip") as database:
        alembic_cli("downgrade", PREVIOUS_REVISION, dsn=database.dsn)
        conn = _connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('app_commands')")
                assert cur.fetchone() == (None,), "downgrade left the table behind"
        finally:
            conn.close()
        alembic_cli("upgrade", "head", dsn=database.dsn)
        conn = _connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('app_commands') IS NOT NULL")
                assert cur.fetchone() == (True,)
        finally:
            conn.close()
