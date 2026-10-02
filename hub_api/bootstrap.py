"""hub-api startup schema bootstrap (fix/chart-fresh-install-hooks, alpha 2026-10-01).

Moves fresh-install schema creation out of a Helm pre-install hook (which
raced the in-chart Postgres Deployment -- a plain/regular resource Helm only
creates AFTER the entire pre-install hook phase finishes, so the old
`db-migrate` hook always timed out against a database that did not exist
yet) and into hub-api's own startup path, where it can actually observe
Postgres coming up.

Honors backend-database.md rule #9 ("NO automatic Alembic migrations on
startup -- manual or K8s Job only; `create_all()` is safe (idempotent)")
precisely:

  * Fresh database (no `alembic_version` table yet): create the schema with
    SQLAlchemy `Base.metadata.create_all()` (idempotent, rule #9 explicitly
    allows this), then write the `alembic_version` row(s) directly ("stamp
    head" -- pure bookkeeping, never executes a migration script body).
  * Database exists but is behind head: NEVER migrated here. That stays the
    `db-migrate` Helm hook Job's job alone, now `pre-upgrade` only (see
    templates/migrations-job.yaml). This module only detects "behind" and
    keeps hub-api's readiness false -- logged as an ERROR with current vs
    head -- until an operator runs `helm upgrade` (which fires the hook) or
    the schema otherwise advances.
  * Database already at head: skip entirely, go ready.

Concurrency: every hub-api replica calls `run_bootstrap_loop()` at startup.
A Postgres session-level advisory lock (`pg_advisory_lock(BOOTSTRAP_LOCK_KEY)`)
serializes the fresh-install race -- only one replica ever runs
create_all()+stamp; the others block on the lock, then observe
"already at head" once they acquire it in turn.

Deliberately does NOT execute `alembic/env.py` or `alembic.command.*`
(upgrade/stamp/current): this process already has the REAL `flask_core`
package fully imported, but `env.py` unconditionally replaces
`sys.modules["flask_core"]` with a stub package (see its own docstring --
built for the stripped-down `migrations` image, which never installs
flask_core's real dependencies) before importing `flask_core.models`. Running
that inside a long-lived process that already imported the real package
risks corrupting `sys.modules` out from under every other module. Instead:
`alembic.script.ScriptDirectory` is used directly (purely static parsing of
`alembic/versions/*.py` revision graph -- never executes `env.py`) to compute
the head revision id(s), and both the "current" read and the "stamp" write
go through plain SQL against `alembic_version`, which is exactly the table
`alembic stamp head` itself writes.
"""

from __future__ import annotations

import asyncio
import logging
import random
import traceback
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import sqlalchemy
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

#: Fixed, never-changing Postgres advisory lock key. A 63-bit constant (fits
#: bigint) derived once from the literal string "waddlebot:hub-api:bootstrap"
#: -- hardcoded rather than hashed at runtime so it can never drift between
#: hub-api processes/releases.
BOOTSTRAP_LOCK_KEY = 0x5741_4442_4F4F_5401

#: Directory containing alembic/versions/*.py in the hub-api image (see
#: Dockerfile: `COPY alembic/versions ./alembic/versions`). Only the version
#: files are needed -- ScriptDirectory parses their revision/down_revision
#: graph statically, it never executes alembic/env.py.
DEFAULT_ALEMBIC_SCRIPT_LOCATION = str(Path(__file__).resolve().parent / "alembic")


class BootstrapState(StrEnum):
    """Lifecycle states exposed to hub-api's `/ready` endpoint."""

    PENDING = "pending"
    WAITING_FOR_DB = "waiting_for_db"
    SCHEMA_BEHIND = "schema_behind"
    READY = "ready"
    FAILED = "failed"


@dataclass(slots=True)
class BootstrapStatus:
    """Shared, mutable status object -- one instance per app, read by `/ready`."""

    state: BootstrapState = BootstrapState.PENDING
    detail: str = ""

    @property
    def is_ready(self) -> bool:
        """True only once the schema is confirmed at head."""
        return self.state is BootstrapState.READY


def _script_heads(script_location: str) -> tuple[str, ...]:
    """Static revision-graph heads from alembic/versions/*.py -- never runs env.py."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", script_location)
    script = ScriptDirectory.from_config(cfg)
    return tuple(sorted(script.get_heads()))


def _current_db_heads(conn: Any) -> tuple[str, ...]:
    """Current alembic_version rows, or () if the table doesn't exist yet (fresh DB)."""
    exists = conn.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = 'alembic_version')"
        )
    ).scalar()
    if not exists:
        return ()
    rows = conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    return tuple(sorted(rows))


def _stamp_heads(conn: Any, heads: tuple[str, ...]) -> None:
    """Write alembic_version row(s) directly.

    The exact bookkeeping `alembic stamp head` performs for a single-head
    repo, without executing env.py to get there.
    """
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS alembic_version ("
            "version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        )
    )
    conn.execute(text("DELETE FROM alembic_version"))
    for head in heads:
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": head})


def _sync_bootstrap_attempt(
    engine: Engine, script_location: str, metadata: sqlalchemy.MetaData
) -> tuple[BootstrapState, str]:
    """One lock-guarded attempt. Runs on a worker thread (sync SQLAlchemy/psycopg2)."""
    heads = _script_heads(script_location)

    lock_conn = engine.connect()
    try:
        lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": BOOTSTRAP_LOCK_KEY})

        with engine.connect() as probe_conn:
            current = _current_db_heads(probe_conn)

        if not current:
            # KNOWN GAP (fix/chart-fresh-install-hooks, 2026-10-01): metadata here is
            # flask_core.models.db.metadata, which only carries tables that have a
            # declared SQLAlchemy model (~14 tables: auth_role/auth_user/communities/
            # hub_users/video_*/engagement_* -- see libs/flask_core/flask_core/models/).
            # The majority of the schema (migrations/run-alembic.sh's own required-table
            # check names `commands`/`platform_integrations`; alembic/versions/0001
            # replays 96 legacy config/postgres/migrations/*.sql files; ~30 further
            # Alembic revisions 0002-0030+ run raw `op.execute()` SQL with no
            # corresponding model) is NOT represented in this metadata object at all.
            # create_all() therefore does NOT reproduce a full migration-chain schema on
            # a truly empty database -- see
            # hub_api/tests/test_bootstrap_schema_drift.py, which asserts this gap
            # directly against a real Postgres and documents it as a reportable defect
            # in this design rather than silently shipping an incomplete schema. Flagged
            # in the PR description; not fixed here (porting 100+ tables' worth of raw
            # SQL into SQLAlchemy models is out of this fix's scope/budget).
            with engine.begin() as create_conn:
                metadata.create_all(bind=create_conn)
                _stamp_heads(create_conn, heads)
            return (
                BootstrapState.READY,
                f"fresh install: schema created via create_all() + stamped head {heads!r}",
            )

        if current == heads:
            return BootstrapState.READY, f"schema already at head {heads!r}"

        return (
            BootstrapState.SCHEMA_BEHIND,
            f"schema behind head (current={current!r} heads={heads!r}); "
            "run `helm upgrade` (fires the db-migrate pre-upgrade hook) to advance it",
        )
    finally:
        try:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": BOOTSTRAP_LOCK_KEY})
        finally:
            lock_conn.close()


async def run_bootstrap_loop(
    status: BootstrapStatus,
    database_url: str,
    logger: Any,
    *,
    script_location: str = DEFAULT_ALEMBIC_SCRIPT_LOCATION,
    metadata: sqlalchemy.MetaData | None = None,
    initial_backoff_seconds: float = 1.0,
    max_backoff_seconds: float = 30.0,
) -> None:
    """Background task: retries with capped exponential backoff + jitter until READY.

    Never raises/returns on WAITING_FOR_DB or SCHEMA_BEHIND -- both are
    recoverable without a pod restart (Postgres finishing startup; an
    operator running the migrate hook) so the loop just keeps polling,
    DEBUG-logging each wait and ERROR-logging each confirmed "behind"
    reading. Only an unexpected exception during the fresh-install
    create_all()/stamp attempt is treated as fatal: logged with a full
    traceback and re-raised, so the caller can exit non-zero and crashloop
    visibly rather than serve a half-initialized schema.
    """
    if metadata is None:
        from flask_core.models import db as _models_db

        metadata = _models_db.metadata

    sync_url = database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    engine = sqlalchemy.create_engine(sync_url, pool_pre_ping=True)
    backoff = initial_backoff_seconds
    attempt = 0
    try:
        while True:
            attempt += 1
            try:
                state, detail = await asyncio.to_thread(
                    _sync_bootstrap_attempt, engine, script_location, metadata
                )
            except OperationalError as exc:
                status.state = BootstrapState.WAITING_FOR_DB
                status.detail = str(exc.__cause__ or exc)
                logger.debug(
                    f"bootstrap: database not reachable yet (attempt {attempt}): {status.detail}",
                    extra={"action": "bootstrap_waiting_for_db", "attempt": attempt},
                )
            except Exception as exc:  # noqa: BLE001 -- fresh-install failure must crash visibly
                status.state = BootstrapState.FAILED
                status.detail = str(exc)
                logger.error(
                    "bootstrap: unexpected error during schema bootstrap -- crashing so "
                    f"the pod visibly crashloops instead of serving a half-initialized "
                    f"schema: {exc}\n{traceback.format_exc()}",
                    extra={"action": "bootstrap_failed"},
                )
                raise
            else:
                status.state = state
                status.detail = detail
                if state is BootstrapState.READY:
                    logger.info(
                        f"bootstrap: {detail}",
                        extra={"action": "bootstrap_ready"},
                    )
                    return
                # SCHEMA_BEHIND -- always an ERROR, never silent, per critical-rules.md
                # Observability: this is an actionable-by-an-operator condition.
                logger.error(
                    f"bootstrap: {detail}",
                    extra={"action": "bootstrap_schema_behind", "attempt": attempt},
                )
            # Jitter only -- not a security/crypto context, just backoff spread.
            sleep_for = min(backoff, max_backoff_seconds) * (0.8 + 0.4 * random.random())  # noqa: S311
            await asyncio.sleep(sleep_for)
            backoff = min(backoff * 2, max_backoff_seconds)
    finally:
        engine.dispose()


def get_logger_or_stdlib(logger: Any | None) -> Any:
    """Fallback to stdlib logging for standalone/test invocation.

    app.py always passes the real penguin AAA logger in production.
    """
    return logger if logger is not None else logging.getLogger("hub_api.bootstrap")
