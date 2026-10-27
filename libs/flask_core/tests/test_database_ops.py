"""AsyncDAL operational tests against real on-disk SQLite (plus an adapter double for raw SQL).

Complements test_database.py (#280 commit/rollback regressions): read-replica routing,
bulk insert, executesql, transactions, URI conversion and shutdown.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from flask_core.database import AsyncDAL, _convert_uri_for_pydal, init_database


@pytest.fixture
async def dal(tmp_path: Path) -> AsyncDAL:
    d = AsyncDAL("sqlite://ops.db", folder=str(tmp_path), pool_size=1, migrate=True)
    d.define_table("widgets", d.Field("name"), d.Field("qty", "integer", default=0))
    d.define_table("uniq", d.Field("code", unique=True))
    await d.count_async(d.dal.widgets.id > 0)  # force lazy CREATE TABLE
    await d.count_async(d.dal.uniq.id > 0)
    return d


class TestCrud:
    async def test_insert_select_update_delete_bulk(self, dal):
        w = dal.dal.widgets
        first = await dal.insert_async(w, name="a", qty=1)
        assert first
        ids = await dal.bulk_insert_async(w, [{"name": "b", "qty": 2}, {"name": "c", "qty": 3}])
        assert len(ids) == 2
        rows = await dal.select_async(dal.dal(w.id > 0), orderby=w.id)
        assert [r.name for r in rows] == ["a", "b", "c"]
        assert await dal.count_async(w.qty >= 2) == 2
        assert await dal.update_async(w.name == "a", qty=10) == 1
        assert (await dal.select_async(dal.dal(w.name == "a")))[0].qty == 10
        assert await dal.delete_async(w.name == "c") == 1
        assert await dal.count_async(w.id > 0) == 2

    async def test_write_errors_roll_back_and_reraise(self, dal):
        u = dal.dal.uniq
        await dal.insert_async(u, code="a")
        await dal.insert_async(u, code="b")
        with pytest.raises(Exception):
            await dal.insert_async(u, code="a")  # UNIQUE violation
        with pytest.raises(Exception):
            await dal.bulk_insert_async(u, [{"code": "a"}])
        with pytest.raises(Exception):
            await dal.update_async(u.code == "b", code="a")
        with pytest.raises(Exception):
            await dal.count_async("this is (not valid sql")
        # connection still usable after every rolled-back failure
        assert await dal.insert_async(u, code="c")
        assert await dal.count_async(u.id > 0) == 3

    async def test_attribute_access_proxies_to_underlying_dal(self, dal):
        assert dal.widgets is dal.dal.widgets  # __getattr__ proxy


class TestExecuteSql:
    async def test_executesql_async_read_and_write(self, dal):
        await dal.executesql_async("INSERT INTO widgets(name, qty) VALUES ('raw', 5);")
        rows = await dal.executesql_async("SELECT name, qty FROM widgets WHERE name = 'raw';")
        assert list(rows[0]) == ["raw", 5]

    async def test_executesql_async_error_rolls_back(self, dal):
        with pytest.raises(Exception):
            await dal.executesql_async("SELECT * FROM no_such_table;")
        assert await dal.count_async(dal.dal.widgets.id > 0) == 0

    async def test_executesql_async_with_params(self, dal):
        await dal.executesql_async("INSERT INTO widgets(name, qty) VALUES (?, ?);", ["p", 9])
        rows = await dal.executesql_async("SELECT qty FROM widgets WHERE name = ?;", ["p"])
        assert rows[0][0] == 9


class _Cursor:
    def __init__(self, rows, description):
        self._rows, self.description = rows, description

    def fetchall(self):
        if isinstance(self._rows, Exception):
            raise self._rows
        return self._rows


class _Adapter:
    """psycopg2-shaped adapter: records what execute() was asked to run."""

    def __init__(self, rows=(), description=None, fail=None):
        self.cursor = _Cursor(list(rows) if not isinstance(rows, Exception) else rows, description)
        self.calls: list[tuple] = []
        self.fail = fail
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def execute(self, sql, params):
        self.calls.append((sql, params))
        if self.fail:
            raise self.fail


class TestExecuteWithAdapter:
    """`execute()` talks to the DB-API adapter directly ($N -> %s, type coercion)."""

    async def test_placeholder_and_type_conversion(self, dal):
        uid = uuid.uuid4()
        dal.dal._adapter = _Adapter(rows=[(1, "x")], description=[("id",), ("name",)])
        rows = await dal.execute(
            "SELECT * FROM t WHERE a = $1 AND b = $2 AND c = $3 AND d = $4 AND e = $10 AND f = $11",
            [uid, {"k": 1}, [1, 2], "plain", "ten", "eleven"] + [None] * 5,
        )
        sql, params = dal.dal._adapter.calls[0]
        assert "$" not in sql and sql.count("%s") == 6
        assert params[0] == str(uid)
        assert json.loads(params[1]) == {"k": 1} and json.loads(params[2]) == [1, 2]
        assert params[3] == "plain"
        assert rows == [{"id": 1, "name": "x"}]
        assert dal.dal._adapter.commits == 1 and dal.dal._adapter.rollbacks == 0

    async def test_no_params_passes_none(self, dal):
        dal.dal._adapter = _Adapter(rows=[], description=None)
        assert await dal.execute("UPDATE t SET a = 1") == []
        assert dal.dal._adapter.calls == [("UPDATE t SET a = 1", None)]

    async def test_no_resultset_swallows_fetch_error(self, dal):
        dal.dal._adapter = _Adapter(rows=RuntimeError("no results to fetch"), description=None)
        assert await dal.execute("INSERT INTO t VALUES (1)") == []

    async def test_driver_error_rolls_back_and_reraises(self, dal):
        dal.dal._adapter = _Adapter(fail=RuntimeError("syntax error"))
        with pytest.raises(RuntimeError):
            await dal.execute("SELEKT 1", [1])
        assert dal.dal._adapter.rollbacks == 1 and dal.dal._adapter.commits == 0


class TestTransactionAsync:
    async def test_commit_path(self, dal):
        w = dal.dal.widgets
        async with dal.transaction_async() as tx:
            assert tx is dal
            await dal.insert_async(w, name="in-tx")
        assert await dal.count_async(w.name == "in-tx") == 1

    async def test_error_inside_block_reraises(self, dal):
        with pytest.raises(ValueError, match="boom"):
            async with dal.transaction_async():
                raise ValueError("boom")


class TestReadReplica:
    async def test_reads_use_replica_dal_writes_use_primary(self, tmp_path):
        d = AsyncDAL("sqlite://main.db", folder=str(tmp_path), pool_size=1,
                     read_replica_uri="sqlite://main.db")
        assert d.read_dal is not None and d.read_dal is not d.dal
        d.define_table("widgets", d.Field("name"))
        await d.insert_async(d.dal.widgets, name="r")
        assert await d.count_async(d.read_dal.widgets.name == "r") == 1
        rows = await d.select_async(d.read_dal(d.read_dal.widgets.id > 0))
        assert [r.name for r in rows] == ["r"]


class TestShutdown:
    async def test_close_async_releases_connections(self, tmp_path):
        d = AsyncDAL("sqlite://c.db", folder=str(tmp_path), pool_size=1)
        d.define_table("widgets", d.Field("name"))
        await d.count_async(d.dal.widgets.id > 0)
        await d.close_async()  # regression: raised AttributeError (_pydal_db_instances_ is thread-local)
        with pytest.raises(RuntimeError):
            d.executor.submit(lambda: None)

    async def test_close_async_with_read_replica(self, tmp_path):
        d = AsyncDAL("sqlite://c2.db", folder=str(tmp_path), pool_size=1, read_replica_uri="sqlite://c2.db")
        d.define_table("widgets", d.Field("name"))
        await d.count_async(d.dal.widgets.id > 0)
        await d.close_async()


class TestUriHelpers:
    @pytest.mark.parametrize("uri,expected", [
        ("postgresql://u:p@h/db", "postgres://u:p@h/db"),
        ("postgres://u:p@h/db", "postgres://u:p@h/db"),
        ("sqlite://x.db", "sqlite://x.db"),
        ("", ""),
        (None, None),
    ])
    def test_convert_uri(self, uri, expected):
        assert _convert_uri_for_pydal(uri) == expected

    async def test_convert_only_first_scheme_occurrence(self):
        assert _convert_uri_for_pydal("postgresql://h/postgresql://x") == "postgres://h/postgresql://x"

    async def test_init_database_builds_asyncdal(self, tmp_path):
        d = init_database("sqlite://init.db", pool_size=1, folder=str(tmp_path), read_replica_uri="sqlite://init.db")
        try:
            assert isinstance(d, AsyncDAL) and d.read_dal is not None
        finally:
            d.executor.shutdown(wait=False)
