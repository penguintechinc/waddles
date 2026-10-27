"""Unit tests for bootstrap.py's state machine.

Mocked SQLAlchemy engine/connection, no real Postgres (see
test_bootstrap_schema_drift.py for the static create_all()-never-again
guard).

USER DECISION (2026-10-01, fix/chart-fresh-install-hooks): the schema always
comes from the db-migrate Helm hook Job (now post-install too) -- bootstrap.py
no longer creates or stamps schema itself, it only observes `alembic_version`.
Covers the three states this design calls out explicitly: fresh/empty
database (WAITING_FOR_MIGRATION, never creates anything), behind head
(SCHEMA_BEHIND, never migrates), at head (READY), plus lock serialization
(advisory lock acquired before work and released after, including on an
exception mid-attempt).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from bootstrap import (
    BootstrapState,
    BootstrapStatus,
    _script_heads,
    _sync_bootstrap_attempt,
    run_bootstrap_loop,
)


def test_script_heads_reads_real_alembic_versions_dir() -> None:
    """Sanity check against the real alembic/versions/ directory -- no mocking, no DB."""
    from pathlib import Path

    repo_alembic = Path(__file__).resolve().parents[2] / "alembic"
    heads = _script_heads(str(repo_alembic))
    assert isinstance(heads, tuple)
    assert len(heads) >= 1


def _make_conn_mock(current_rows: list[str] | None) -> MagicMock:
    """A connection mock answering `_current_db_heads`'s two queries.

    `.execute(...).scalar()`/`.scalars().all()` chain answers: table-exists
    check, then version_num rows. Also wired as its own context manager
    (`__enter__` returns itself) so `with engine.connect() as conn:` binds to
    this same mock, not an auto-generated child mock.
    """
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False

    def _execute(stmt, params=None):  # noqa: ANN001 -- test double signature
        text = str(stmt)
        result = MagicMock()
        if "information_schema.tables" in text:
            result.scalar.return_value = current_rows is not None
        elif "SELECT version_num" in text:
            result.scalars.return_value.all.return_value = current_rows or []
        else:
            result.scalar.return_value = None
        return result

    conn.execute.side_effect = _execute
    return conn


def _sql_calls(mock_conn: MagicMock) -> list[str]:
    """Normalize a mock's `.execute(text(...), ...)` call history to bare SQL strings.

    `sqlalchemy.TextClause` doesn't define `__eq__`, so comparing `call()`
    objects containing it directly is unreliable; compare the rendered SQL
    text instead.
    """
    return [str(c.args[0]) for c in mock_conn.execute.call_args_list]


class TestSyncBootstrapAttempt:
    """`_sync_bootstrap_attempt` -- the single lock-guarded, synchronous attempt."""

    def _engine_with(self, probe_conn: MagicMock, lock_conn: MagicMock) -> MagicMock:
        engine = MagicMock()
        connects = [lock_conn, probe_conn]
        engine.connect.side_effect = lambda: connects.pop(0)
        return engine

    @patch("bootstrap._script_heads", return_value=("0030_bundle_app_schemas",))
    def test_fresh_database_waits_for_migration_never_creates_anything(self, _mock_heads) -> None:
        probe_conn = _make_conn_mock(current_rows=None)  # table doesn't exist -> fresh
        lock_conn = MagicMock()
        engine = self._engine_with(probe_conn, lock_conn)

        state, detail = _sync_bootstrap_attempt(engine, "/fake/alembic")

        assert state is BootstrapState.WAITING_FOR_MIGRATION
        assert "db-migrate" in detail
        # Never creates/alters schema -- no engine.begin() (the old
        # create_all()/stamp transaction) is ever opened.
        engine.begin.assert_not_called()
        # Lock acquired on lock_conn before the probe, released after -- same connection.
        calls = _sql_calls(lock_conn)
        assert any("pg_advisory_lock" in c for c in calls)
        assert any("pg_advisory_unlock" in c for c in calls)

    @patch("bootstrap._script_heads", return_value=("0030_bundle_app_schemas",))
    def test_schema_behind_never_migrates(self, _mock_heads) -> None:
        probe_conn = _make_conn_mock(current_rows=["0014_wave1a_bundle_seeds"])
        lock_conn = MagicMock()
        engine = self._engine_with(probe_conn, lock_conn)

        state, detail = _sync_bootstrap_attempt(engine, "/fake/alembic")

        assert state is BootstrapState.SCHEMA_BEHIND
        assert "0014_wave1a_bundle_seeds" in detail
        assert "0030_bundle_app_schemas" in detail
        engine.begin.assert_not_called()

    @patch("bootstrap._script_heads", return_value=("0030_bundle_app_schemas",))
    def test_at_head_skips_entirely(self, _mock_heads) -> None:
        probe_conn = _make_conn_mock(current_rows=["0030_bundle_app_schemas"])
        lock_conn = MagicMock()
        engine = self._engine_with(probe_conn, lock_conn)

        state, detail = _sync_bootstrap_attempt(engine, "/fake/alembic")

        assert state is BootstrapState.READY
        assert "already at head" in detail
        engine.begin.assert_not_called()

    @patch("bootstrap._script_heads", return_value=("0030_bundle_app_schemas",))
    def test_lock_released_even_if_work_raises(self, _mock_heads) -> None:
        """Lock serialization must not leak a held session lock on an unexpected failure."""
        lock_conn = MagicMock()
        engine = MagicMock()
        engine.connect.side_effect = [lock_conn, RuntimeError("probe connection boom")]

        with pytest.raises(RuntimeError, match="probe connection boom"):
            _sync_bootstrap_attempt(engine, "/fake/alembic")

        unlock_calls = [c for c in _sql_calls(lock_conn) if "pg_advisory_unlock" in c]
        assert unlock_calls, "advisory lock must be released even when the attempt raises"
        lock_conn.close.assert_called_once()


class TestRunBootstrapLoop:
    """`run_bootstrap_loop` -- the background retry/backoff wrapper."""

    @pytest.mark.asyncio
    async def test_ready_on_first_attempt_returns_without_looping(self) -> None:
        status = BootstrapStatus()
        logger = MagicMock()
        with (
            patch(
                "bootstrap._sync_bootstrap_attempt",
                return_value=(BootstrapState.READY, "already at head"),
            ),
            patch("sqlalchemy.create_engine", return_value=MagicMock()),
        ):
            await run_bootstrap_loop(status, "postgresql://u:p@h:5432/d", logger)

        assert status.is_ready
        logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_fresh_install_logs_info_not_error(self) -> None:
        """WAITING_FOR_MIGRATION is routine on every fresh install -- INFO, never ERROR."""
        status = BootstrapStatus()
        logger = MagicMock()
        attempts = [
            (BootstrapState.WAITING_FOR_MIGRATION, "fresh install: schema not created yet"),
            (BootstrapState.WAITING_FOR_MIGRATION, "fresh install: schema not created yet"),
            (BootstrapState.READY, "already at head"),
        ]
        with (
            patch("bootstrap._sync_bootstrap_attempt", side_effect=attempts),
            patch("sqlalchemy.create_engine", return_value=MagicMock()),
            patch("asyncio.sleep", return_value=None),
        ):
            await run_bootstrap_loop(
                status,
                "postgresql://u:p@h:5432/d",
                logger,
                initial_backoff_seconds=0.001,
                max_backoff_seconds=0.01,
            )

        assert status.is_ready
        logger.error.assert_not_called()
        assert logger.info.call_count >= 2  # one per WAITING_FOR_MIGRATION reading + ready

    @pytest.mark.asyncio
    async def test_schema_behind_retries_then_recovers(self) -> None:
        """Simulates an operator running the migrate hook mid-retry.

        Never crashes, never gives up, eventually flips ready.
        """
        status = BootstrapStatus()
        logger = MagicMock()
        attempts = [
            (BootstrapState.SCHEMA_BEHIND, "current=() heads=(x,)"),
            (BootstrapState.SCHEMA_BEHIND, "current=() heads=(x,)"),
            (BootstrapState.READY, "already at head"),
        ]
        with (
            patch("bootstrap._sync_bootstrap_attempt", side_effect=attempts),
            patch("sqlalchemy.create_engine", return_value=MagicMock()),
            patch("asyncio.sleep", return_value=None),
        ):
            await run_bootstrap_loop(
                status,
                "postgresql://u:p@h:5432/d",
                logger,
                initial_backoff_seconds=0.001,
                max_backoff_seconds=0.01,
            )

        assert status.is_ready
        assert logger.error.call_count == 2  # one ERROR per SCHEMA_BEHIND reading

    @pytest.mark.asyncio
    async def test_db_unreachable_waits_then_recovers(self) -> None:
        status = BootstrapStatus()
        logger = MagicMock()
        attempts = [
            OperationalError("stmt", {}, Exception("connection refused")),
            (BootstrapState.READY, "already at head"),
        ]
        with (
            patch("bootstrap._sync_bootstrap_attempt", side_effect=attempts),
            patch("sqlalchemy.create_engine", return_value=MagicMock()),
            patch("asyncio.sleep", return_value=None),
        ):
            await run_bootstrap_loop(
                status,
                "postgresql://u:p@h:5432/d",
                logger,
                initial_backoff_seconds=0.001,
            )

        assert status.is_ready
        assert status.state is BootstrapState.READY

    @pytest.mark.asyncio
    async def test_unexpected_error_crashes_visibly(self) -> None:
        """A real bug in the schema check must propagate.

        Never silently retried forever as if it were a recoverable waiting
        state.
        """
        status = BootstrapStatus()
        logger = MagicMock()
        with (
            patch(
                "bootstrap._sync_bootstrap_attempt",
                side_effect=RuntimeError("permission denied for table alembic_version"),
            ),
            patch("sqlalchemy.create_engine", return_value=MagicMock()),
        ):
            with pytest.raises(RuntimeError, match="permission denied"):
                await run_bootstrap_loop(status, "postgresql://u:p@h:5432/d", logger)

        assert status.state is BootstrapState.FAILED
        logger.error.assert_called_once()
        assert "permission denied" in logger.error.call_args[0][0]
