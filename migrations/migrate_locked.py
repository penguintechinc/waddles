"""Run the migrations entrypoint (run-alembic.sh) under a cluster-wide Postgres advisory lock.

The migrations image runs from two call sites: hub-api's `db-migrate` initContainer (every
replica, fresh install AND upgrade) and the `pre-upgrade` db-migrate hook Job. Several replicas
may start at once, so the WHOLE run -- `alembic upgrade head` plus the role/grant reconciles that
follow it -- is serialized behind one session-level `pg_advisory_lock`. alembic/env.py's own
lock (20250001) only covers `upgrade head`; concurrent GRANT/ALTER ROLE reconciles would still
race ("tuple concurrently updated"). A later holder finds the schema at head and the reconciles
are idempotent, so every replica converges.

Fail-loud: a missing DATABASE_URL, an unreachable database after the bounded wait, or a lock
not won within MIGRATE_LOCK_TIMEOUT_SECONDS exits non-zero (the kubelet / Job retries). The lock
is session-scoped: if this process dies, Postgres drops the connection and releases it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import sqlalchemy as sa
from sqlalchemy.pool import NullPool

#: Whole-run lock key. MUST differ from alembic/env.py's 20250001: env.py takes its lock on a
#: second session while this one is held, and session advisory locks only re-enter per session.
RUN_LOCK_KEY = 20250002
DEFAULT_RUN_SCRIPT = "/app/run.sh"
EXIT_CONFIG = 2
EXIT_DB_UNAVAILABLE = 3
EXIT_LOCK_TIMEOUT = 4


def _log(msg: str) -> None:
    """Write one prefixed line to stderr (CLI runner output; never carries the DSN)."""
    print(f"migrate-locked: {msg}", file=sys.stderr, flush=True)


def _env_seconds(name: str, default: int) -> int:
    """Read a positive integer seconds value from the environment, failing loud if malformed."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value}")
    return value


def _engine_url(dsn: str) -> str:
    """Normalize a postgresql:// DSN to the psycopg2 driver SQLAlchemy uses in this image."""
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+psycopg2://", 1)
    return dsn


def _connect_with_retry(engine: sa.Engine, wait_seconds: int) -> sa.Connection:
    """Open one connection, retrying every 2s until `wait_seconds` elapse (Postgres may be booting)."""
    deadline = time.monotonic() + wait_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            return engine.connect()
        except sa.exc.OperationalError as exc:
            if time.monotonic() >= deadline:
                raise
            _log(f"database not ready (attempt {attempt}): {type(exc).__name__}; retrying in 2s")
            time.sleep(2)


def run(argv: list[str]) -> int:
    """Acquire the run lock, execute the migrations script, release the lock, return its exit code."""
    script = argv[1] if len(argv) > 1 else DEFAULT_RUN_SCRIPT
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        _log("ERROR: DATABASE_URL is empty -- refusing to run migrations without the migrator DSN")
        return EXIT_CONFIG
    try:
        wait_seconds = _env_seconds("MIGRATE_DB_WAIT_SECONDS", 120)
        lock_timeout = _env_seconds("MIGRATE_LOCK_TIMEOUT_SECONDS", 900)
    except ValueError as exc:
        _log(f"ERROR: {exc}")
        return EXIT_CONFIG

    engine = sa.create_engine(_engine_url(dsn), poolclass=NullPool)
    try:
        conn = _connect_with_retry(engine, wait_seconds)
    except sa.exc.OperationalError as exc:
        _log(f"ERROR: database not reachable after {wait_seconds}s: {type(exc).__name__}")
        return EXIT_DB_UNAVAILABLE

    with conn:
        conn.execute(sa.text("SELECT set_config('lock_timeout', :v, false)"), {"v": f"{lock_timeout}s"})
        started = time.monotonic()
        _log(f"waiting for advisory lock {RUN_LOCK_KEY} (timeout {lock_timeout}s)")
        try:
            conn.execute(sa.text("SELECT pg_advisory_lock(:k)"), {"k": RUN_LOCK_KEY})
        except sa.exc.OperationalError as exc:
            _log(f"ERROR: advisory lock {RUN_LOCK_KEY} not acquired within {lock_timeout}s: {type(exc).__name__}")
            return EXIT_LOCK_TIMEOUT
        # Session lock survives commit; never sit idle-in-transaction for the whole run.
        conn.commit()
        _log(f"advisory lock {RUN_LOCK_KEY} acquired after {time.monotonic() - started:.1f}s; running {script}")
        try:
            rc = subprocess.run(["/bin/sh", script], check=False).returncode  # fixed argv, no shell
        finally:
            conn.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": RUN_LOCK_KEY})
            conn.commit()
            _log(f"advisory lock {RUN_LOCK_KEY} released")
    _log(f"{script} exited {rc}")
    return rc


if __name__ == "__main__":
    sys.exit(run(sys.argv))
