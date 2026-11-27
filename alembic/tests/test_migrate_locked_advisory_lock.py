"""migrations/migrate_locked.py: the advisory lock serializes concurrent migration runs (real Postgres).

regression: fix/db-migrate-fresh-install-ordering moved the schema migration into hub-api's
initContainer, so N replicas (plus the pre-upgrade Job) start migration runs at once. The
wrapper must run the migrations script under ONE cluster-wide pg_advisory_lock: two runs never
overlap, a failed run releases the lock and propagates its exit code, a lock not won in time
fails loud without running the script, and a missing DSN never runs anything.

Overlap detector: the stand-in script does an atomic `mkdir` of a marker dir, sleeps, then
removes it -- a concurrent second run's `mkdir` fails (exit 9). The negative control proves
the detector fires when the same script runs concurrently WITHOUT the wrapper.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, empty_postgres

_REPO = Path(__file__).resolve().parents[2]
_WRAPPER = _REPO / "migrations" / "migrate_locked.py"
_LOCK_KEY = 20250002

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason="docker CLI not available in this environment")


@pytest.fixture(scope="module")
def pg() -> Iterator[PgTestDatabase]:
    """One empty Postgres 17 container for this module."""
    with empty_postgres("migrate-lock") as db:
        yield db


def _script(tmp: Path, body: str) -> Path:
    """Write a /bin/sh stand-in for run-alembic.sh."""
    path = tmp / "run.sh"
    path.write_text(f"set -e\n{body}\n")
    return path


def _overlap_script(tmp: Path) -> Path:
    """Script that fails (exit 9) if another copy is running concurrently."""
    marker = tmp / "running"
    return _script(tmp, f'mkdir "{marker}" || exit 9\nsleep 1.5\nrmdir "{marker}"')


def _env(dsn: str | None, **extra: str) -> dict[str, str]:
    """Child env: inherited PATH etc., explicit DSN (or none), extra knobs."""
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    if dsn is not None:
        env["DATABASE_URL"] = dsn
    env.update(extra)
    return env


def _spawn(dsn: str | None, script: Path, **extra: str) -> subprocess.Popen[str]:
    """Start the wrapper as a separate process (its own Postgres session)."""
    return subprocess.Popen(  # fixed argv, test-only
        [sys.executable, str(_WRAPPER), str(script)],
        env=_env(dsn, **extra), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def _finish(proc: subprocess.Popen[str]) -> tuple[int, str]:
    """Wait (bounded) and return (exit code, combined output)."""
    out, _ = proc.communicate(timeout=60)
    return proc.returncode, out


def _lock_is_free(dsn: str) -> bool:
    """True when another session can take (and then drops) the run lock immediately."""
    engine = sa.create_engine(dsn.replace("postgresql://", "postgresql+psycopg2://", 1), poolclass=sa.pool.NullPool)
    with engine.connect() as conn:
        got = bool(conn.execute(sa.text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY}).scalar())
        if got:
            conn.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
    return got


def test_detector_fires_without_the_lock(tmp_path: Path) -> None:
    script = _overlap_script(tmp_path)
    procs = [subprocess.Popen(["/bin/sh", str(script)]) for _ in range(3)]  # fixed argv
    codes = [p.wait(timeout=30) for p in procs]
    assert 9 in codes, f"overlap detector never fired unlocked (codes={codes}) -- test is not a gate"


def test_concurrent_runs_are_serialized(pg: PgTestDatabase, tmp_path: Path) -> None:
    script = _overlap_script(tmp_path)
    procs = [_spawn(pg.dsn, script) for _ in range(3)]
    results = [_finish(p) for p in procs]
    assert len(results) == 3, "zero runs examined"
    for rc, out in results:
        assert rc == 0, out
        assert f"advisory lock {_LOCK_KEY} acquired" in out and f"advisory lock {_LOCK_KEY} released" in out
    assert _lock_is_free(pg.dsn)


def test_failed_run_propagates_exit_and_releases_lock(pg: PgTestDatabase, tmp_path: Path) -> None:
    rc, out = _finish(_spawn(pg.dsn, _script(tmp_path, "exit 7")))
    assert rc == 7, out
    assert _lock_is_free(pg.dsn), "lock leaked after a failed run"


def test_lock_timeout_fails_loud_without_running(pg: PgTestDatabase, tmp_path: Path) -> None:
    ran = tmp_path / "ran"
    script = _script(tmp_path, f'touch "{ran}"')
    engine = sa.create_engine(pg.dsn.replace("postgresql://", "postgresql+psycopg2://", 1), poolclass=sa.pool.NullPool)
    with engine.connect() as holder:
        holder.execute(sa.text("SELECT pg_advisory_lock(:k)"), {"k": _LOCK_KEY})
        holder.commit()
        try:
            rc, out = _finish(_spawn(pg.dsn, script, MIGRATE_LOCK_TIMEOUT_SECONDS="1"))
        finally:
            holder.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
            holder.commit()
    assert rc == 4, out
    assert "not acquired" in out and not ran.exists()


def test_missing_dsn_runs_nothing(tmp_path: Path) -> None:
    ran = tmp_path / "ran"
    rc, out = _finish(_spawn(None, _script(tmp_path, f'touch "{ran}"')))
    assert rc == 2, out
    assert "DATABASE_URL is empty" in out and not ran.exists()


def test_unreachable_db_fails_loud(tmp_path: Path) -> None:
    ran = tmp_path / "ran"
    dsn = "postgresql://nobody:x@127.0.0.1:1/none"
    rc, out = _finish(_spawn(dsn, _script(tmp_path, f'touch "{ran}"'), MIGRATE_DB_WAIT_SECONDS="1"))
    assert rc == 3, out
    assert not ran.exists()
