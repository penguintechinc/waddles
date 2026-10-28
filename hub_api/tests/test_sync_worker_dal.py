"""Tests for `services/sync_worker_dal.py` -- the DAL the standalone sync CronJobs run on.

regression: every sync worker `main()` (role-sync, event-sync, Odoo) built a penguin-dal 0.4.0
`AsyncDB` and read `.dal` off it. `AsyncDB` has no `.dal`; its `__getattr__` treats the name as
a TABLE and raises `TableNotFoundError: Table 'dal' not found in database`, so every CronJob run
crashed before reconciling a row. The old suites faked that object as `SimpleNamespace(dal=...)`
-- inventing the attribute the real class lacks -- so they stayed green. `AsyncDBShapedDouble`
below reproduces the REAL failure shape, and `block_penguin_dal_asyncdb()` is what each
worker's `main()` test installs so a reintroduced `install_dal.dal` fails loudly.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from flask_core.database import AsyncDAL
from penguin_dal.exceptions import TableNotFoundError
from pydal import DAL

from services import sync_worker_dal as swd
from services.community_connections import get_decrypted_tokens
from services.schema import bind_platform_tables


class AsyncDBShapedDouble:
    """A penguin-dal `AsyncDB` look-alike: any non-underscore attribute is treated as a TABLE.

    Mirrors `penguin_dal.db.AsyncDB.__getattr__` (`_get_table(name)` ->
    `TableNotFoundError(name)`), including its `_`-prefix `AttributeError` carve-out. It
    deliberately has NO `dal` attribute -- exactly like the real class.
    """

    def __getattr__(self, name: str) -> Any:
        """Raise the real `TableNotFoundError` for any table-looking attribute."""
        if name.startswith("_"):
            raise AttributeError(name)
        raise TableNotFoundError(name)


def block_penguin_dal_asyncdb(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `bundle_install_dal.build_install_dal` hand back the real-shaped `AsyncDB` double.

    A sync worker `main()` that goes back to `build_install_dal(...)` + `.dal` now dies with
    the production error (`TableNotFoundError: Table 'dal' not found`) instead of passing.
    """
    import services.bundle_install_dal as bid

    async def _build(_url: str, pool_size: int = 1) -> AsyncDBShapedDouble:
        return AsyncDBShapedDouble()

    monkeypatch.setattr(bid, "build_install_dal", _build)


def fake_worker_dal(dal: Any) -> Any:
    """A drop-in for `open_worker_dal` yielding a flask_core-`AsyncDAL`-shaped object over `dal`.

    `AsyncDAL` genuinely has a `.dal` (the raw pydal DAL), so `SimpleNamespace(dal=...)` is a
    faithful stand-in HERE -- unlike for a penguin-dal `AsyncDB`.
    """

    @contextmanager
    def _open(_url: str) -> Iterator[Any]:
        yield SimpleNamespace(dal=dal)

    return _open


def seed_twitch_connection(dal: Any, community_id: int, access_token: str) -> None:
    """Persist an active, UNencrypted `community_oauth` twitch connection row (no key needed).

    `is_encrypted=False` is `get_decrypted_tokens`' plain-passthrough lane, so the test needs
    no `CREDENTIAL_ENCRYPTION_KEY` -- the point here is the DAL shape, not the cipher.
    """
    bind_platform_tables(dal, migrate=True)
    for table_name in dal.tables:  # force lazy CREATE TABLE on this thread, as the fixtures do
        dal(dal[table_name]).count()
    dal.platform_integrations.insert(
        platform="twitch",
        integration_type="community_oauth",
        community_id=community_id,
        access_token=access_token,
        is_encrypted=False,
        is_active=True,
    )
    dal.commit()


def test_asyncdb_double_has_no_dal_attribute() -> None:
    """The double reproduces the production failure the old `SimpleNamespace(dal=...)` hid."""
    with pytest.raises(TableNotFoundError, match="Table 'dal' not found"):
        _ = AsyncDBShapedDouble().dal


class TestOpenWorkerDal:
    """`open_worker_dal` against a real sqlite file -- the real `init_database` + pydal path."""

    def test_yields_async_dal_wrapping_a_raw_pydal_dal(
        self, bar_citizen_db: Any, tmp_path: Any
    ) -> None:
        url = f"sqlite://{tmp_path / 'bar_citizen_test.db'}"
        with swd.open_worker_dal(url) as async_dal:
            assert isinstance(async_dal, AsyncDAL)
            # The engines call `dal(query).select()` / `dal.tables` / `dal.define_table`.
            assert isinstance(async_dal.dal, DAL)
            swd.bind_reference_tenants(async_dal.dal)
            assert async_dal.dal(async_dal.dal.tenants.id > 0).count() == 2

    async def test_async_dal_satisfies_get_decrypted_tokens(
        self, bar_citizen_db: Any, tmp_path: Any
    ) -> None:
        """The token lookup (`.dal` + `select_async`) works on the yielded object, for real."""
        fixture_dal, community_id, _t, _g = bar_citizen_db
        seed_twitch_connection(fixture_dal, community_id, "tok-real")
        url = f"sqlite://{tmp_path / 'bar_citizen_test.db'}"
        with swd.open_worker_dal(url) as async_dal:
            tokens = await get_decrypted_tokens(async_dal, community_id, "twitch")
            assert tokens is not None and tokens.access_token == "tok-real"  # noqa: S105
            assert await get_decrypted_tokens(async_dal, community_id, "discord") is None

    async def test_token_lookup_rejects_a_penguin_dal_asyncdb(self) -> None:
        """`get_decrypted_tokens` needs the flask_core `AsyncDAL` -- an `AsyncDB` is the old bug."""
        with pytest.raises(TableNotFoundError, match="Table 'dal' not found"):
            await get_decrypted_tokens(AsyncDBShapedDouble(), 1, "twitch")

    def test_error_rolls_back_uncommitted_work_instead_of_committing_it(
        self, bar_citizen_db: Any, tmp_path: Any
    ) -> None:
        """A failed pass must not persist half its writes (pydal `close()` commits)."""
        fixture_dal, _c, _t, _g = bar_citizen_db
        before = fixture_dal(fixture_dal.tenants.id > 0).count()
        url = f"sqlite://{tmp_path / 'bar_citizen_test.db'}"
        with pytest.raises(RuntimeError, match="boom"), swd.open_worker_dal(url) as async_dal:
            swd.bind_reference_tenants(async_dal.dal)
            async_dal.dal.tenants.insert(slug="half-applied")
            raise RuntimeError("boom")
        fixture_dal.rollback()  # end this connection's snapshot so it re-reads the file
        assert fixture_dal(fixture_dal.tenants.id > 0).count() == before

    def test_executor_is_shut_down_on_exit(self, bar_citizen_db: Any, tmp_path: Any) -> None:
        url = f"sqlite://{tmp_path / 'bar_citizen_test.db'}"
        with swd.open_worker_dal(url) as async_dal:
            executor = async_dal.executor
        with pytest.raises(RuntimeError, match="after shutdown"):
            executor.submit(lambda: None)

    def test_close_failure_is_logged_not_raised(
        self, bar_citizen_db: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing `close()` must not mask a finished pass, and the executor still shuts down."""
        logged: list[str] = []
        monkeypatch.setattr(swd, "log_db_error", lambda _log, msg, _exc: logged.append(msg))
        url = f"sqlite://{tmp_path / 'bar_citizen_test.db'}"
        with swd.open_worker_dal(url) as async_dal:
            executor = async_dal.executor

            def _boom() -> None:
                raise OSError("close failed")

            monkeypatch.setattr(async_dal.dal, "close", _boom)
        assert logged == ["sync worker DAL close failed"]
        with pytest.raises(RuntimeError, match="after shutdown"):
            executor.submit(lambda: None)

    def test_rollback_failure_is_logged_and_original_error_still_raised(
        self, bar_citizen_db: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        logged: list[str] = []
        monkeypatch.setattr(swd, "log_db_error", lambda _log, msg, _exc: logged.append(msg))
        url = f"sqlite://{tmp_path / 'bar_citizen_test.db'}"
        with pytest.raises(ValueError, match="original"), swd.open_worker_dal(url) as async_dal:

            def _boom() -> None:
                raise OSError("rollback failed")

            monkeypatch.setattr(async_dal.dal, "rollback", _boom)
            raise ValueError("original")
        assert logged == ["sync worker DAL rollback failed after error"]


class TestBindReferenceTenants:
    def test_is_idempotent_and_defines_the_tenants_columns(self) -> None:
        dal = DAL("sqlite:memory", lazy_tables=True)
        swd.bind_reference_tenants(dal)
        first = dal.tenants
        swd.bind_reference_tenants(dal)  # second call must not redefine
        assert dal.tenants is first
        assert {"slug", "display_name", "is_global", "is_active", "config"} <= set(
            dal.tenants.fields
        )
        dal.close()
