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

import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The revision immediately before 0020 -- everything from here forward is
#: plain, self-contained Python/SQL against the four bootstrapped tables
#: below, no legacy-baseline dependency.
_STAMP_REVISION = "0019_kick_app"

DOCKER_AVAILABLE = shutil.which("docker") is not None

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
    name TEXT
);
CREATE TABLE hub_users (
    id SERIAL PRIMARY KEY
);
CREATE TABLE app_catalog (
    app_id VARCHAR(255) PRIMARY KEY
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

    @property
    def dsn(self) -> str:
        """`postgresql://` DSN, the exact form `DATABASE_URL`/`psycopg2.connect` accept."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.dbname}"


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(container: str, user: str, dbname: str, timeout_s: float = 60.0) -> None:
    """Block until `dbname` actually accepts a real query.

    `pg_isready` alone is insufficient: the official Postgres image
    accepts connections briefly during its own first-run `initdb`
    bootstrap, then restarts once more before `POSTGRES_DB` actually
    exists -- `pg_isready` returns success during that brief window,
    racily reporting "ready" before the target database exists at all
    (`FATAL: database "..." does not exist`). A real `SELECT 1` against
    the actual database is the only check that can't false-positive here.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
            ["docker", "exec", container, "psql", "-U", user, "-d", dbname, "-c", "SELECT 1"],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise TimeoutError(f"postgres container {container!r} never became ready within {timeout_s}s")


@contextmanager
def migrated_postgres(name_suffix: str) -> Iterator[PgTestDatabase]:
    """Start a uniquely-named/ported Postgres 17 container, migrate it to `head`, yield, tear down.

    Args:
        name_suffix: Distinguishes this container from any other test's
            (or any other concurrently running agent's) container --
            never a shared/fixed name.
    """
    container = f"waddles-migtest-{name_suffix}"
    port = _free_port()
    db = PgTestDatabase(
        host="127.0.0.1", port=port, user="waddlebot", password="testpass123", dbname="waddlebot"
    )
    subprocess.run(  # noqa: S603 -- fixed argv, no shell
        ["docker", "rm", "-f", container], capture_output=True, check=False
    )
    subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [
            "docker", "run", "-d", "--name", container,
            "-e", f"POSTGRES_USER={db.user}",
            "-e", f"POSTGRES_PASSWORD={db.password}",
            "-e", f"POSTGRES_DB={db.dbname}",
            "-p", f"{port}:5432",
            "postgres:17-bookworm",
        ],
        capture_output=True,
        check=True,
    )
    try:
        _wait_ready(container, db.user, db.dbname)
        subprocess.run(  # noqa: S603 -- fixed argv, no shell
            ["docker", "exec", "-i", container, "psql", "-U", db.user, "-d", db.dbname],
            input=_BOOTSTRAP_SQL,
            text=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(  # noqa: S603 -- fixed argv, no shell
            [sys.executable, "-m", "alembic", "stamp", _STAMP_REVISION],
            cwd=REPO_ROOT,
            env={**os.environ, "DATABASE_URL": db.dsn},
            capture_output=True,
            check=True,
        )
        upgrade = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT,
            env={**os.environ, "DATABASE_URL": db.dsn},
            capture_output=True,
            check=False,
        )
        if upgrade.returncode != 0:
            raise RuntimeError(
                f"alembic upgrade head failed:\nstdout={upgrade.stdout}\nstderr={upgrade.stderr}"
            )
        yield db
    finally:
        subprocess.run(  # noqa: S603 -- fixed argv, no shell
            ["docker", "rm", "-f", container], capture_output=True, check=False
        )


def alembic_cli(*args: str, dsn: str) -> subprocess.CompletedProcess[str]:
    """Run one `alembic` subcommand against `dsn`, repo root as cwd. Raises on nonzero exit."""
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": dsn},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
    return result
