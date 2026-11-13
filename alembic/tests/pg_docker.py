"""Ephemeral, real-Postgres-container harness for tests that need actual execution.

Every sibling `test_00NN_*.py` in this directory mocks `alembic.op.execute`
and asserts emitted SQL text (see `test_0019_kick_app.py`'s own docstring:
"this repo has no pytest-level fixture that runs Alembic against a real
Postgres in CI"). `bundle_active_set_changes`'s own guarantees -- a
trigger actually fires per write, and the primary-side `safe_seq`
horizon never includes a still-in-flight transaction's row -- cannot be
verified against mocked SQL text; they require a real Postgres, a real
trigger, and a real second, concurrently open, uncommitted transaction.

**Why this bootstraps a minimal schema instead of running the full
baseline.** `0001_baseline_from_sql_migrations` replays all 96
`config/postgres/migrations/*.sql` files, which depend on roles and
seed data `config/postgres/init.sql` provisions in a specific order as
part of the full docker-compose dev stack (Postgres roles, several
early tables, and later baseline files, interleaved) -- reproducing
that whole chain from a bare container hits pre-existing ordering
issues unrelated to this migration (verified manually: init.sql's own
`ai_insights` table creation fails outside that stack because it
expects `communities` to already exist). Fixing that chain is out of
this migration's scope. Instead: create only the four tables migrations
0020+ actually reference by FK (`tenants`, `communities`, `hub_users`,
`app_catalog`, with just the columns those migrations' own `CREATE
TABLE`/comments name), `alembic stamp 0019_kick_app` (the revision
immediately before the first migration this schema supports), then run
`alembic upgrade head` for real -- migrations 0020 through 0028 execute
exactly as they would in a full environment, including 0028's own
triggers/tables/grants.
"""

from __future__ import annotations

import atexit
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The revision immediately before 0020 -- everything from here forward is
#: plain, self-contained Python/SQL against the four bootstrapped tables
#: below, no legacy-baseline dependency.
_STAMP_REVISION = "0019_kick_app"

DOCKER_AVAILABLE = shutil.which("docker") is not None

_SERVICE_ROLES_PATH = REPO_ROOT / "scripts" / "db" / "service_roles.py"


def load_service_roles_module():  # type: ignore[no-untyped-def]
    """Import `scripts/db/service_roles.py` by path (shared with 0055 and run-alembic.sh)."""
    spec = importlib.util.spec_from_file_location("waddles_service_roles_tests", _SERVICE_ROLES_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def throwaway_service_role_passwords() -> dict[str, str]:
    """Random per-role passwords for a disposable test database -- never a real credential."""
    roles = load_service_roles_module()
    return dict(roles.generate_passwords(roles.load_catalog()))


def _default_service_role_passwords_json() -> str:
    return json.dumps(throwaway_service_role_passwords())


_BOOTSTRAP_SQL = """
CREATE TABLE tenants (
    id SERIAL PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    logo_url TEXT,
    config JSONB
);
CREATE TABLE communities (
    id SERIAL PRIMARY KEY,
    name TEXT,
    tenant_id INTEGER,  -- 0045 trigger reads it (legacy NOT NULL column)
    -- identity_resolution_service's "current member" predicate reads these two
    -- (config/postgres/migrations/000_create_base_schema.sql)
    is_active BOOLEAN DEFAULT TRUE,
    deleted_at TIMESTAMP
);
CREATE TABLE hub_users (
    id SERIAL PRIMARY KEY,
    -- identity_resolution_service never returns a deactivated (erased) hub user
    is_active BOOLEAN DEFAULT TRUE,
    -- a PII column outside 0043's column-scoped reader grant (uuid, id), so
    -- `SELECT *` as waddles_bundle_reader is still denied (test_0032).
    username TEXT,
    -- identity_resolution_service.resolve_display_names reads it (hub profile name)
    display_name TEXT
);
-- 0044_connector_pii_reader_role column-grants on this legacy table
-- (config/postgres/migrations/000_create_base_schema.sql); only the
-- columns it grants/joins on.
CREATE TABLE hub_user_identities (
    id SERIAL PRIMARY KEY,
    hub_user_id INTEGER NOT NULL REFERENCES hub_users(id) ON DELETE CASCADE,
    platform VARCHAR(50) NOT NULL,
    platform_user_id VARCHAR(255) NOT NULL,
    platform_username VARCHAR(255)
);
-- `stages` (legacy SQL 071): 0047_builtin_handler_paths rewrites the per-stage
-- `entrypoint` strings inside it, so the bootstrap table needs the column
-- (same JSONB type/default as 071). Mirrored in pr-validation.yml's bootstrap.
CREATE TABLE app_catalog (
    app_id VARCHAR(255) PRIMARY KEY,
    stages JSONB DEFAULT '{}'::jsonb,
    -- 0095_bundle_disabled_apps_view projects app_id WHERE status = 'disabled'.
    status TEXT DEFAULT 'active'
);
-- 0043_hub_users_identity_uuid creates a view over community_members (a
-- legacy config/postgres table, same minimal-bootstrap convention): only the
-- columns the view projects/joins on. 0050_bundle_reputation_store also ALTERs
-- this table -- is_active/left_at/removed_at are the columns it, its grants
-- and its tests touch.
CREATE TABLE community_members (
    id SERIAL PRIMARY KEY,
    community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,
    user_id VARCHAR(255),
    platform VARCHAR(50),
    platform_user_id VARCHAR(255),
    display_name VARCHAR(255),
    -- identity_resolution_service's "current member" predicate (not left, not removed):
    -- 000_create_base_schema.sql (is_active, left_at) + 0012_schema_drift_columns (removed_at)
    is_active BOOLEAN DEFAULT TRUE,
    left_at TIMESTAMP,
    removed_at TIMESTAMP,
    -- 0079_bundle_platform_table column-scopes GRANTs over these baseline columns
    -- (000_create_base_schema.sql): role is the only UPDATE-able one.
    role VARCHAR(50),
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- 0039_event_sync_enabled is the first Alembic migration to FK into
-- calendar_events -- a legacy table owned by config/postgres/migrations/
-- (never Alembic, see this module's own docstring on why the full legacy
-- baseline isn't replayed here). Same minimal-bootstrap convention as
-- tenants/communities/hub_users/app_catalog above: only the column(s)
-- migrations 0020+ actually FK against (just `id`), not the real table's
-- full column set.
CREATE TABLE calendar_events (
    id SERIAL PRIMARY KEY
);
-- 0092_leaderboard_config_toggles ALTERs this legacy table (never Alembic-created).
CREATE TABLE community_leaderboard_config (
    id SERIAL PRIMARY KEY
);
-- 0063_seal_aad_version ALTERs this legacy table (config/postgres/migrations/
-- 030_platform_integrations.sql, never Alembic): same columns as the real DDL minus its
-- indexes/scope CHECK. Mirrored in pr-validation.yml's bootstrap -- keep both in sync.
CREATE TABLE platform_integrations (
    id SERIAL PRIMARY KEY,
    platform VARCHAR(50) NOT NULL,
    integration_type VARCHAR(20) NOT NULL,
    community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES hub_users(id) ON DELETE CASCADE,
    access_token TEXT,
    refresh_token TEXT,
    client_id VARCHAR(255),
    client_secret TEXT,
    token_type VARCHAR(50) DEFAULT 'Bearer',
    expires_at TIMESTAMP WITH TIME ZONE,
    scopes TEXT[],
    config_data JSONB,
    is_active BOOLEAN DEFAULT TRUE,
    is_encrypted BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    created_by_user_id INTEGER REFERENCES hub_users(id) ON DELETE SET NULL,
    updated_by_user_id INTEGER REFERENCES hub_users(id) ON DELETE SET NULL,
    CONSTRAINT platform_integrations_unique_bot
        UNIQUE (platform, integration_type, community_id, user_id)
);
-- 0069_pi_definer_functions audits every community-secret read into this legacy table
-- (config/postgres/migrations/000_create_base_schema.sql): same columns as the real DDL.
-- Mirrored in pr-validation.yml's bootstrap -- keep both in sync.
CREATE TABLE credential_access_log (
    id BIGSERIAL PRIMARY KEY,
    db_user VARCHAR(255) NOT NULL DEFAULT current_user,
    integration_id INTEGER,
    platform VARCHAR(50),
    community_id INTEGER,
    action VARCHAR(50) NOT NULL,
    success BOOLEAN NOT NULL DEFAULT TRUE,
    detail TEXT,
    accessed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


@dataclass(slots=True, frozen=True)
class PgTestDatabase:
    """Connection parameters for one ephemeral, fully-migrated test container."""

    host: str
    port: int
    user: str
    password: str
    dbname: str
    #: Per-service LOGIN role passwords this container was migrated with (role -> password),
    #: so a test can authenticate AS a service role. Empty when the caller never migrated.
    service_role_passwords: Mapping[str, str] = field(default_factory=dict)
    #: Docker container name backing this database (for `docker exec`); per-process unique.
    container: str = ""

    @property
    def dsn(self) -> str:
        """`postgresql://` DSN, the exact form `DATABASE_URL`/`psycopg2.connect` accept."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.dbname}"


def container_name(name_suffix: str) -> str:
    """Per-process container name: concurrent sessions/agents never `docker rm -f` each other's."""
    return f"waddles-migtest-{name_suffix}-{os.getpid()}"


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


#: Overall readiness deadline (seconds) for one container; override via env on a loaded host.
_READY_TIMEOUT_ENV = "WADDLES_PG_READY_TIMEOUT_S"
#: Per-probe (`docker exec ... psql`) timeout (seconds) so one wedged exec can't hang the run.
_PROBE_TIMEOUT_ENV = "WADDLES_PG_PROBE_TIMEOUT_S"
_DEFAULT_READY_TIMEOUT_S = 120.0
_DEFAULT_PROBE_TIMEOUT_S = 10.0
_LOG_TAIL_LINES = 50

#: Containers started by this process and not yet removed -- the atexit finalizer's worklist.
_LIVE_CONTAINERS: set[str] = set()


def _env_seconds(name: str, default: float) -> float:
    """Read a positive float of seconds from env `name`; fail loudly on a malformed value."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} is not a number of seconds") from exc
    if value <= 0:
        raise ValueError(f"{name}={raw!r} must be > 0")
    return value


def _container_log_tail(container: str) -> str:
    """Last `_LOG_TAIL_LINES` lines of `docker logs` for a failure message (bounded, never raises)."""
    try:
        result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
            ["docker", "logs", "--tail", str(_LOG_TAIL_LINES), container],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return "<docker logs timed out>"
    return (result.stdout + result.stderr).strip() or "<empty>"


def _remove_container(container: str) -> None:
    """`docker rm -f` with a bounded timeout; drops it from the live set either way."""
    try:
        subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
            ["docker", "rm", "-f", container], capture_output=True, check=False, timeout=60
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"pg_docker: docker rm -f {container} timed out; container may leak\n")
    finally:
        _LIVE_CONTAINERS.discard(container)


def _cleanup_live_containers() -> None:
    """atexit finalizer: remove any container a crashed/interrupted test never tore down."""
    for container in sorted(_LIVE_CONTAINERS):
        _remove_container(container)


atexit.register(_cleanup_live_containers)


def _start_container(container: str, db: PgTestDatabase) -> None:
    """`docker run` a fresh Postgres 17 container and register it for guaranteed teardown."""
    _remove_container(container)
    _LIVE_CONTAINERS.add(container)
    subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [
            "docker", "run", "-d", "--name", container,
            "--label", "waddles.test=pg_docker",
            "-e", f"POSTGRES_USER={db.user}",
            "-e", f"POSTGRES_PASSWORD={db.password}",
            "-e", f"POSTGRES_DB={db.dbname}",
            "-p", f"{db.port}:5432",
            "postgres:17-bookworm",
        ],
        capture_output=True,
        check=True,
        timeout=300,
    )


def _wait_ready(
    container: str,
    user: str,
    dbname: str,
    timeout_s: float | None = None,
    probe_timeout_s: float | None = None,
) -> None:
    """Block until `dbname` actually accepts a real query, or raise with the container log tail.

    `pg_isready` alone is insufficient: the official Postgres image
    accepts connections briefly during its own first-run `initdb`
    bootstrap, then restarts once more before `POSTGRES_DB` actually
    exists -- a real `SELECT 1` against the actual database is the only
    check that can't false-positive here. Each probe is bounded by
    `probe_timeout_s` (env `WADDLES_PG_PROBE_TIMEOUT_S`, default 10s) and the
    whole wait by `timeout_s` (env `WADDLES_PG_READY_TIMEOUT_S`, default 120s),
    so a loaded host fails loudly instead of hanging forever.
    """
    total = timeout_s if timeout_s is not None else _env_seconds(
        _READY_TIMEOUT_ENV, _DEFAULT_READY_TIMEOUT_S
    )
    per_probe = probe_timeout_s if probe_timeout_s is not None else _env_seconds(
        _PROBE_TIMEOUT_ENV, _DEFAULT_PROBE_TIMEOUT_S
    )
    deadline = time.monotonic() + total
    attempts = 0
    last_error = "<no probe ran>"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        attempts += 1
        try:
            result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
                ["docker", "exec", container, "psql", "-U", user, "-d", dbname, "-c", "SELECT 1"],
                capture_output=True,
                text=True,
                check=False,
                timeout=min(per_probe, max(remaining, 0.1)),
            )
        except subprocess.TimeoutExpired:
            last_error = f"probe timed out after {per_probe}s"
        else:
            if result.returncode == 0:
                return
            last_error = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        time.sleep(min(1.0, max(deadline - time.monotonic(), 0)))
    raise TimeoutError(
        f"postgres container {container!r} never became ready within {total}s "
        f"({attempts} probes, per-probe timeout {per_probe}s); last error: {last_error}\n"
        f"--- docker logs --tail {_LOG_TAIL_LINES} {container} ---\n"
        f"{_container_log_tail(container)}"
    )


@contextmanager
def migrated_postgres(name_suffix: str) -> Iterator[PgTestDatabase]:
    """Start a uniquely-named/ported Postgres 17 container, migrate it to `head`, yield, tear down.

    Args:
        name_suffix: Distinguishes this container from any other test's
            (or any other concurrently running agent's) container --
            never a shared/fixed name.
    """
    container = container_name(name_suffix)
    port = _free_port()
    service_role_passwords = throwaway_service_role_passwords()
    db = PgTestDatabase(
        host="127.0.0.1",
        port=port,
        user="waddlebot",
        password="testpass123",
        dbname="waddlebot",
        service_role_passwords=service_role_passwords,
        container=container,
    )
    try:
        _start_container(container, db)
        _wait_ready(container, db.user, db.dbname)
        subprocess.run(  # noqa: S603 -- fixed argv, no shell
            ["docker", "exec", "-i", container, "psql", "-U", db.user, "-d", db.dbname],
            input=_BOOTSTRAP_SQL,
            text=True,
            capture_output=True,
            check=True,
        )
        # fix/no-empty-kept-secrets -- 0032_bundle_reader_role.py's upgrade() now
        # refuses an empty DB_READER_PASSWORD (previously silently provisioned
        # waddles_bundle_reader with no usable password). Most callers of this
        # harness (every real-Postgres test outside alembic/tests/
        # test_0032_bundle_reader_role.py itself) don't care about this role at
        # all, so `setdefault` supplies a throwaway value here -- never a real
        # credential, this container is destroyed at context-manager exit --
        # while still letting a caller that DOES care (stages its own value in
        # `os.environ` beforehand) override it, same as every other env var.
        migration_env = {**os.environ, "DATABASE_URL": db.dsn}
        migration_env.setdefault("DB_READER_PASSWORD", "pg-docker-harness-default-reader-pw")
        # 0055_per_service_db_roles refuses to provision service roles without
        # credentials (fail loud) -- supply a disposable per-role set.
        migration_env.setdefault(
            "WADDLES_DB_SERVICE_ROLE_PASSWORDS", json.dumps(service_role_passwords)
        )
        subprocess.run(  # noqa: S603 -- fixed argv, no shell
            [sys.executable, "-m", "alembic", "stamp", _STAMP_REVISION],
            cwd=REPO_ROOT,
            env=migration_env,
            capture_output=True,
            check=True,
        )
        upgrade = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT,
            env=migration_env,
            capture_output=True,
            check=False,
        )
        if upgrade.returncode != 0:
            raise RuntimeError(
                f"alembic upgrade head failed:\nstdout={upgrade.stdout}\nstderr={upgrade.stderr}"
            )
        yield db
    finally:
        _remove_container(container)


@contextmanager
def empty_postgres(name_suffix: str) -> Iterator[PgTestDatabase]:
    """Start a genuinely empty Postgres 17 container (no bootstrap SQL, no stamp, no
    migrations) -- used only by `hub_api/tests/test_bootstrap_schema_drift.py` to measure
    what `hub_api/bootstrap.py`'s `create_all()` path alone produces on a truly fresh
    database, as opposed to `migrated_postgres()`'s full Alembic-chain result above.
    """
    container = container_name(name_suffix)
    port = _free_port()
    db = PgTestDatabase(
        host="127.0.0.1",
        port=port,
        user="waddlebot",
        password="testpass123",
        dbname="waddlebot",
        container=container,
    )
    try:
        _start_container(container, db)
        _wait_ready(container, db.user, db.dbname)
        yield db
    finally:
        _remove_container(container)


def bootstrap_minimal_schema(name_suffix: str, db: PgTestDatabase) -> None:
    """Load the minimal FK bootstrap into an `empty_postgres(name_suffix)` container + stamp.

    Same shape `migrated_postgres` uses (and the CI alembic-migration-chain job): enough
    legacy tables for migrations 0020+ without replaying the 96-file SQL baseline -- for
    tests that only need to reach (or fail at) a late revision quickly.
    """
    subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [
            "docker", "exec", "-i", db.container or container_name(name_suffix),
            "psql", "-U", db.user, "-d", db.dbname,
        ],
        input=_BOOTSTRAP_SQL,
        text=True,
        capture_output=True,
        check=True,
    )
    alembic_cli("stamp", _STAMP_REVISION, dsn=db.dsn)


def alembic_cli(
    *args: str, dsn: str, env_overrides: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one `alembic` subcommand against `dsn`, repo root as cwd. Raises on nonzero exit.

    Same `DB_READER_PASSWORD` default as `migrated_postgres` above -- an
    `upgrade`/`downgrade` round-trip that crosses 0032_bundle_reader_role.py
    re-runs its (now fail-loud-on-empty) `upgrade()`, and most callers of this
    helper don't stage their own value. `WADDLES_DB_SERVICE_ROLE_PASSWORDS` (0055)
    is defaulted the same way; pass `env_overrides` to stage specific values.
    """
    env = {**os.environ, "DATABASE_URL": dsn, **(env_overrides or {})}
    env.setdefault("DB_READER_PASSWORD", "pg-docker-harness-default-reader-pw")
    env.setdefault("WADDLES_DB_SERVICE_ROLE_PASSWORDS", _default_service_role_passwords_json())
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
    return result
