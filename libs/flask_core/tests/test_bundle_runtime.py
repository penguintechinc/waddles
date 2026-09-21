"""Tests for `flask_core.bundle_runtime` -- the DAL + tenant/community accessor
stateful App Bundles use from inside their frozen `transform()`/action-entrypoint
bodies.

Loaded directly via `importlib`, bypassing `flask_core/__init__.py`, same
pattern as `test_stage_runner.py`/`test_stream_pipeline.py` -- this module
only depends on stdlib (`contextvars`, `dataclasses`), never pydal/quart/
authlib, so it has no business triggering the heavy package `__init__`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Boolean, Column, Integer, MetaData, String, Table, text

from penguin_dal import AsyncDB


def _load_bundle_runtime_module() -> Any:
    """Load bundle_runtime.py directly -- see module docstring."""
    module_name = "flask_core.bundle_runtime"
    if module_name in sys.modules:
        return sys.modules[module_name]
    src = Path(__file__).resolve().parent.parent / "flask_core" / "bundle_runtime.py"
    spec = importlib.util.spec_from_file_location(module_name, src)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bundle_runtime() -> Any:
    return _load_bundle_runtime_module()


@pytest.fixture(autouse=True)
def _reset_dal_after_each_test(bundle_runtime: Any) -> Any:
    """Every test starts and ends with no DAL bound -- state must never leak across tests."""
    bundle_runtime.reset_bundle_dal_for_tests()
    yield
    bundle_runtime.reset_bundle_dal_for_tests()


class _FakeDal:
    """Stand-in for `AsyncDAL` -- these tests only verify identity passthrough, not DB behavior."""


class TestBundleDal:
    def test_get_before_set_raises(self, bundle_runtime: Any) -> None:
        with pytest.raises(bundle_runtime.BundleRuntimeError, match="no DAL bound"):
            bundle_runtime.get_bundle_dal()

    def test_set_then_get_returns_same_instance(self, bundle_runtime: Any) -> None:
        dal = _FakeDal()
        bundle_runtime.set_bundle_dal(dal)
        assert bundle_runtime.get_bundle_dal() is dal

    def test_set_twice_overwrites(self, bundle_runtime: Any) -> None:
        first, second = _FakeDal(), _FakeDal()
        bundle_runtime.set_bundle_dal(first)
        bundle_runtime.set_bundle_dal(second)
        assert bundle_runtime.get_bundle_dal() is second

    def test_reset_clears_bound_dal(self, bundle_runtime: Any) -> None:
        bundle_runtime.set_bundle_dal(_FakeDal())
        bundle_runtime.reset_bundle_dal_for_tests()
        with pytest.raises(bundle_runtime.BundleRuntimeError):
            bundle_runtime.get_bundle_dal()

    def test_error_message_points_at_set_bundle_dal(self, bundle_runtime: Any) -> None:
        with pytest.raises(bundle_runtime.BundleRuntimeError, match="set_bundle_dal"):
            bundle_runtime.get_bundle_dal()


class TestBundleContext:
    def test_get_outside_block_raises(self, bundle_runtime: Any) -> None:
        with pytest.raises(
            bundle_runtime.BundleRuntimeError, match="no bundle context bound"
        ):
            bundle_runtime.get_bundle_context()

    def test_inside_block_returns_bound_values(self, bundle_runtime: Any) -> None:
        with bundle_runtime.bundle_context(
            tenant="acme-corp", community="42", app_id="waddles.bot.demo.default"
        ) as ctx:
            assert ctx.tenant == "acme-corp"
            assert ctx.community == "42"
            assert ctx.app_id == "waddles.bot.demo.default"
            fetched = bundle_runtime.get_bundle_context()
            assert fetched.tenant == "acme-corp"
            assert fetched.community == "42"
            assert fetched.app_id == "waddles.bot.demo.default"

    def test_tenant_wide_activation_allows_none_community(
        self, bundle_runtime: Any
    ) -> None:
        with bundle_runtime.bundle_context(
            tenant="acme-corp", community=None, app_id="waddles.bot.demo.default"
        ):
            assert bundle_runtime.get_bundle_context().community is None

    def test_context_cleared_after_block_exits(self, bundle_runtime: Any) -> None:
        with bundle_runtime.bundle_context(tenant="t", community=None, app_id="a"):
            pass
        with pytest.raises(bundle_runtime.BundleRuntimeError):
            bundle_runtime.get_bundle_context()

    def test_context_cleared_after_block_raises(self, bundle_runtime: Any) -> None:
        with (
            pytest.raises(ValueError, match="boom"),
            bundle_runtime.bundle_context(tenant="t", community=None, app_id="a"),
        ):
            raise ValueError("boom")
        with pytest.raises(bundle_runtime.BundleRuntimeError):
            bundle_runtime.get_bundle_context()

    def test_nested_blocks_restore_outer_context_on_exit(
        self, bundle_runtime: Any
    ) -> None:
        with bundle_runtime.bundle_context(tenant="outer", community=None, app_id="a"):
            with bundle_runtime.bundle_context(
                tenant="inner", community=None, app_id="b"
            ):
                assert bundle_runtime.get_bundle_context().tenant == "inner"
            assert bundle_runtime.get_bundle_context().tenant == "outer"

    def test_context_is_frozen(self, bundle_runtime: Any) -> None:
        with (
            bundle_runtime.bundle_context(
                tenant="t", community=None, app_id="a"
            ) as ctx,
            pytest.raises(Exception),  # noqa: B017 - dataclass(frozen=True) raises FrozenInstanceError
        ):
            ctx.tenant = "other"  # type: ignore[misc]

    async def test_isolated_across_concurrent_asyncio_tasks(
        self, bundle_runtime: Any
    ) -> None:
        """Two envelopes 'in flight' at once must never see each other's tenant/community.

        Proves the `contextvars.ContextVar` choice (not a plain module
        global) actually matters -- the exact bug class a shared mutable
        global risks if stage runners ever move from sequential draining
        to concurrent per-envelope tasks.
        """
        import asyncio

        seen: dict[str, str | None] = {}

        async def _run(tenant: str, community: str | None) -> None:
            with bundle_runtime.bundle_context(
                tenant=tenant, community=community, app_id="a"
            ):
                await asyncio.sleep(0.01)
                seen[tenant] = bundle_runtime.get_bundle_context().tenant

        await asyncio.gather(
            _run("tenant-a", "1"),
            _run("tenant-b", "2"),
        )
        assert seen == {"tenant-a": "tenant-a", "tenant-b": "tenant-b"}


def _create_widgets_table(conn):  # pragma: no cover
    metadata = MetaData()
    Table(
        "widgets",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("name", String(255), nullable=False),
        Column("active", Boolean, default=True),
    )
    metadata.create_all(conn)


@pytest.fixture
async def dal():
    """An in-memory penguin_dal.AsyncDB with one seeded `widgets` table."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.run_sync(_create_widgets_table)
        await conn.execute(
            text("INSERT INTO widgets (name, active) VALUES ('a', 1), ('b', 0)")
        )
    await db.reflect()
    # Use importlib to get the module (matching test pattern above)
    bundle_runtime = _load_bundle_runtime_module()
    bundle_runtime.set_bundle_dal(db)
    yield db
    bundle_runtime.reset_bundle_dal_for_tests()
    await db.close()


class TestGetSetBundleDalRetyped:
    async def test_get_bundle_dal_returns_asyncdb(self, dal: AsyncDB) -> None:
        bundle_runtime = _load_bundle_runtime_module()
        assert bundle_runtime.get_bundle_dal() is dal
        assert isinstance(bundle_runtime.get_bundle_dal(), AsyncDB)

    async def test_bundle_can_query_via_penguin_dal_query_builder(
        self, dal: AsyncDB
    ) -> None:
        bundle_runtime = _load_bundle_runtime_module()
        rows = await bundle_runtime.get_bundle_dal()(
            bundle_runtime.get_bundle_dal().widgets.active == True  # noqa: E712
        ).select()
        assert len(rows) == 1
        assert rows.first().name == "a"


class TestRawSqlRows:
    async def test_returns_rows_object_with_first_and_dict_access(
        self, dal: AsyncDB
    ) -> None:
        bundle_runtime = _load_bundle_runtime_module()
        rows = await bundle_runtime.raw_sql_rows(
            dal, "SELECT id, name FROM widgets WHERE name = :n", {"n": "a"}
        )
        assert len(rows) == 1
        row = rows.first()
        assert row is not None
        assert row["name"] == "a"
        assert row.name == "a"

    async def test_empty_result_returns_empty_rows(self, dal: AsyncDB) -> None:
        bundle_runtime = _load_bundle_runtime_module()
        rows = await bundle_runtime.raw_sql_rows(
            dal, "SELECT id FROM widgets WHERE name = :n", {"n": "nope"}
        )
        assert len(rows) == 0
        assert rows.first() is None

    async def test_no_params_defaults_to_empty_dict(self, dal: AsyncDB) -> None:
        bundle_runtime = _load_bundle_runtime_module()
        rows = await bundle_runtime.raw_sql_rows(dal, "SELECT id FROM widgets")
        assert len(rows) == 2


class TestRawSqlWrite:
    async def test_insert_commits_and_returns_empty_rows_without_returning(
        self, dal: AsyncDB
    ) -> None:
        bundle_runtime = _load_bundle_runtime_module()
        rows = await bundle_runtime.raw_sql_write(
            dal, "INSERT INTO widgets (name, active) VALUES (:n, 1)", {"n": "c"}
        )
        assert len(rows) == 0
        check = await bundle_runtime.raw_sql_rows(
            dal, "SELECT COUNT(*) AS n FROM widgets"
        )
        assert check.first()["n"] == 3

    async def test_write_persists_across_a_new_connection(
        self, dal: AsyncDB
    ) -> None:
        bundle_runtime = _load_bundle_runtime_module()
        await bundle_runtime.raw_sql_write(
            dal, "UPDATE widgets SET active = 0 WHERE name = :n", {"n": "a"}
        )
        rows = await bundle_runtime.raw_sql_rows(
            dal, "SELECT active FROM widgets WHERE name = :n", {"n": "a"}
        )
        assert rows.first()["active"] == 0
