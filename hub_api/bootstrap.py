"""hub-api startup schema bootstrap (fix/chart-fresh-install-hooks, alpha 2026-10-01).

USER DECISION (2026-10-01, supersedes the create_all()+stamp design this module
originally shipped): the schema ALWAYS comes from the `db-migrate` Helm hook
Job, fresh install included. `flask_core.models.db.metadata` only declares
~14-21 of the 100+ tables the full Alembic migration chain creates (see
`hub_api/tests/test_bootstrap_schema_drift.py`'s git history / this PR's
description) -- `create_all()` on a fresh database therefore produced an
incomplete schema, which the very next `helm upgrade` then failed loudly
against (`run-alembic.sh`'s own required-table check: "migration completed
with required tables missing: commands, platform_integrations"). This module
never creates or stamps schema. It only OBSERVES Alembic's `alembic_version`
table and reports what it sees:

  * No `alembic_version` table yet (truly empty database): logs INFO and
    reports not-ready. Expected and routine on a fresh install between the
    Namespace/Deployments being created and the `db-migrate` post-install
    hook Job completing -- not an error.
  * `alembic_version` exists but is behind the static revision-graph head:
    logs ERROR (current vs head) and reports not-ready. An operator action
    (`helm upgrade`, which fires the `db-migrate` pre-upgrade hook) is
    required to advance it -- this module never runs a migration itself.
  * At head: runs this service's idempotent first-run scripts (currently
    none -- see `_run_first_run_scripts`), then reports ready.

Either not-ready state is retried forever with capped exponential backoff --
both are recoverable without a pod restart (the migrate Job finishing;
an operator running `helm upgrade`), never treated as fatal.

Concurrency: every hub-api replica calls `run_bootstrap_loop()` at startup. A
Postgres session-level advisory lock (`pg_advisory_lock(BOOTSTRAP_LOCK_KEY)`)
serializes the read-and-maybe-run-first-run-scripts sequence across replicas,
even though today's `_run_first_run_scripts` is a no-op -- the lock is the
already-correct home for whatever lands there next.

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
the head revision id(s), and the "current" read goes through plain SQL
against `alembic_version`, the exact table `alembic upgrade`/`alembic stamp`
themselves maintain.
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
    WAITING_FOR_MIGRATION = "waiting_for_migration"
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


def _run_first_run_scripts(engine: Engine) -> None:
    """Idempotent, schema-at-head-only first-run steps -- currently none.

    Extension point, run once the schema is confirmed at head and while the
    advisory lock is still held (so a future addition here is automatically
    serialized across replicas the same way the old create_all()+stamp path
    was). Nothing lives here today: the admin-seed step
    (`config/postgres/migrations/081_seed_default_hub_admin.sql`) ships as
    part of the `db-migrate` Job's own migration chain, which now runs on
    fresh install too (post-install hook, not just pre-upgrade) per the
    2026-10-01 user decision -- so there is no longer a gap between "schema
    exists" and "admin seeded" for this module to fill. Kept as an explicit
    no-op (not simply omitted) so the next genuinely-idempotent first-run
    requirement has an obvious, already-locked home instead of being bolted
    onto `_sync_bootstrap_attempt` directly.
    """
    return


def _sync_bootstrap_attempt(engine: Engine, script_location: str) -> tuple[BootstrapState, str]:
    """One lock-guarded attempt. Runs on a worker thread (sync SQLAlchemy/psycopg2).

    Never creates or alters schema -- only observes `alembic_version` and
    reports what it sees. See module docstring for the three outcomes.
    """
    heads = _script_heads(script_location)

    lock_conn = engine.connect()
    try:
        lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": BOOTSTRAP_LOCK_KEY})

        with engine.connect() as probe_conn:
            current = _current_db_heads(probe_conn)

        if not current:
            return (
                BootstrapState.WAITING_FOR_MIGRATION,
                "fresh install: schema not created yet -- waiting for the db-migrate "
                "Helm hook (post-install) to run `alembic upgrade head`",
            )

        if current == heads:
            _run_first_run_scripts(engine)
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
    initial_backoff_seconds: float = 1.0,
    max_backoff_seconds: float = 30.0,
) -> None:
    """Background task: retries with capped exponential backoff + jitter until READY.

    Never raises/returns on WAITING_FOR_DB, WAITING_FOR_MIGRATION, or
    SCHEMA_BEHIND -- all three are recoverable without a pod restart
    (Postgres finishing startup; the db-migrate hook Job completing; an
    operator running `helm upgrade`), so the loop just keeps polling.
    WAITING_FOR_MIGRATION is routine on every fresh install (logged at INFO,
    never ERROR -- it is not an operator-actionable condition, the hook Job
    is already running). SCHEMA_BEHIND IS operator-actionable (logged at
    ERROR with current vs head, per critical-rules.md Observability). Only an
    unexpected exception during the attempt is treated as fatal: logged with
    a full traceback and re-raised, so the caller can exit non-zero and
    crashloop visibly rather than silently serve a stale/broken state.
    """
    sync_url = database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    engine = sqlalchemy.create_engine(sync_url, pool_pre_ping=True)
    backoff = initial_backoff_seconds
    attempt = 0
    try:
        while True:
            attempt += 1
            try:
                state, detail = await asyncio.to_thread(
                    _sync_bootstrap_attempt, engine, script_location
                )
            except OperationalError as exc:
                status.state = BootstrapState.WAITING_FOR_DB
                status.detail = str(exc.__cause__ or exc)
                logger.debug(
                    f"bootstrap: database not reachable yet (attempt {attempt}): {status.detail}",
                    extra={"action": "bootstrap_waiting_for_db", "attempt": attempt},
                )
            except Exception as exc:  # noqa: BLE001 -- an unexpected failure must crash visibly
                status.state = BootstrapState.FAILED
                status.detail = str(exc)
                logger.error(
                    "bootstrap: unexpected error during schema check -- crashing so "
                    f"the pod visibly crashloops instead of serving a stale status: "
                    f"{exc}\n{traceback.format_exc()}",
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
                if state is BootstrapState.WAITING_FOR_MIGRATION:
                    # Routine, expected on every fresh install -- INFO, not ERROR.
                    logger.info(
                        f"bootstrap: waiting for migrate job (fresh install): {detail}",
                        extra={"action": "bootstrap_waiting_for_migration", "attempt": attempt},
                    )
                else:
                    # SCHEMA_BEHIND -- always an ERROR, never silent, per
                    # critical-rules.md Observability: this is an
                    # actionable-by-an-operator condition.
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
