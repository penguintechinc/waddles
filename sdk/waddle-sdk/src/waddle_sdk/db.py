"""``penguin_dal``-compatible database facade over the WIT ``db`` import.

Spec Sec4.12/D21: this module is the SDK's **one** database surface -- there is
no ``flask_core.database.AsyncDAL`` facade and no pydal facade. Every
statement crosses the WIT boundary through exactly one call,
``wit_world.imports.db.execute(statement, params) -> Rows``, raising
``componentize_py_types.Err(Error)`` on failure. The query builder below runs
entirely in guest Python; only the final ``(statement, params)`` pair and the
returned ``Rows`` record cross the host/guest boundary.

**Binding shapes below are not guessed.** They were confirmed by running
``componentize-py==0.25.1``'s ``bindings`` subcommand directly against the
committed ``wit/waddle-bundle/stage.wit`` (``componentize-py -d
wit/waddle-bundle -w stage bindings <dir>``) and reading the generated
``wit_world/imports/db.py``: the WIT ``variant value`` becomes one
``@dataclass`` per case named ``Value_<PascalCase(case)>`` (``Value_NullValue``,
``Value_BoolValue(value: bool)``, ``Value_IntValue(value: int)``,
``Value_FloatValue(value: float)``, ``Value_TextValue(value: str)``,
``Value_BytesValue(value: bytes)``), all module attributes of the generated
``wit_world.imports.db`` module; ``execute`` returns a ``Rows`` dataclass
(``columns: list[str]``, ``rows: list[list[Value]]``, ``rows_affected: int``)
and raises the generated ``Err`` (a frozen dataclass ``Exception`` subclass
with one attribute, ``value``, holding the ``Error`` variant) on the
``result``'s failure arm. ``componentize_py_types`` (home of ``Err``/``Ok``)
is generated per-build into the component's own working tree and is not
pip-installable, so this module never imports it directly -- it classifies
the raised exception structurally via ``getattr(exc, "value", exc)``, the
same pattern already used by ``waddle_sdk.http`` for the WIT ``http.Error``
variant.

Verified against ``/home/penguin/code/penguin-libs/packages/python-dal/src/
penguin_dal/{db,query,field_proxy,table_proxy}.py``'s public method
signatures (spec D21) -- no ``penguin_dal`` import exists in this file; this
is an independent, call-compatible re-implementation over the WIT ``db``
import.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any
from uuid import UUID


class DALError(Exception):
    """Base exception for this facade -- matches ``penguin_dal.exceptions.DALError``."""


class TableNotFoundError(DALError):
    """Matches ``penguin_dal.exceptions.TableNotFoundError``."""


class ValidationError(DALError):
    """Matches ``penguin_dal.exceptions.ValidationError``."""


def _coerce_param(value: Any) -> Any:
    """Mirror real DAL param conversion before a value is wrapped as a WIT ``value``.

    ``UUID`` -> ``str``, ``dict``/``list`` -> JSON string, ``datetime``/``date``
    -> ISO string. Every other type is wrapped as-is by ``_to_wit_value``.
    """
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict | list):
        return json.dumps(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _to_wit_value(db_mod: Any, value: Any) -> Any:
    """Wrap one coerced Python scalar into the generated WIT ``db.Value`` union.

    See this module's docstring for how the case names below were confirmed.
    """
    coerced = _coerce_param(value)
    if coerced is None:
        return db_mod.Value_NullValue()
    if isinstance(coerced, bool):  # bool is an int subclass -- must check first
        return db_mod.Value_BoolValue(coerced)
    if isinstance(coerced, int):
        return db_mod.Value_IntValue(coerced)
    if isinstance(coerced, float):
        return db_mod.Value_FloatValue(coerced)
    if isinstance(coerced, bytes | bytearray):
        return db_mod.Value_BytesValue(bytes(coerced))
    return db_mod.Value_TextValue(str(coerced))


def _from_wit_value(value: Any) -> Any:
    """Unwrap one generated WIT ``db.Value`` union member back to a Python scalar.

    Dispatches on the dataclass's own class name rather than an ``isinstance``
    check against an imported type, so this function works identically
    against the real generated bindings (only importable inside a component)
    and against this SDK's own host-side test doubles (``tests/wit_fakes.py``),
    which reuse the exact same class names.
    """
    type_name = type(value).__name__
    if type_name == "Value_NullValue":
        return None
    if type_name in ("Value_BoolValue", "Value_IntValue", "Value_FloatValue", "Value_TextValue"):
        return value.value
    if type_name == "Value_BytesValue":
        return bytes(value.value)
    raise NotImplementedError(
        f"waddle-sdk db facade: unrecognized WIT db.value case {type_name!r} -- "
        "the wit/waddle-bundle/stage.wit `db.value` variant has no such case"
    )


def _wit_rows_to_dicts(rows: Any) -> list[dict[str, Any]]:
    """Convert a generated WIT ``db.Rows`` record into a list of plain dicts."""
    return [
        {col: _from_wit_value(val) for col, val in zip(rows.columns, row_values, strict=True)}
        for row_values in rows.rows
    ]


def _cross(statement: str, params: list[Any]) -> tuple[list[dict[str, Any]], int]:
    """The one place every statement crosses the WIT ``db`` import.

    Returns ``(rows_as_dicts, rows_affected)`` -- callers that only need row
    data (``select``) use the first element; callers that only need a count
    (``update``/``delete``) use the second, since the WIT ``Rows.rows_affected``
    field is authoritative for those, not ``len(rows)`` (an ``UPDATE``/``DELETE``
    is not required to also return row data).
    """
    import wit_world  # generated binding -- only resolvable inside a component

    db_mod = wit_world.imports.db
    wit_params = [_to_wit_value(db_mod, p) for p in params]
    try:
        rows = db_mod.execute(statement, wit_params)
    except Exception as exc:  # noqa: BLE001 - see module docstring: Err is structurally classified, never imported
        detail = getattr(exc, "value", exc)
        raise DALError(f"db.execute failed: {detail}") from exc
    return _wit_rows_to_dicts(rows), int(rows.rows_affected)


class Query:
    r"""A combinable WHERE-clause fragment -- matches ``penguin_dal.query.Query``.

    Built as raw SQL text with ``\0`` marking one ``$N`` placeholder slot,
    rendered left-to-right by :meth:`render`.
    """

    __slots__ = ("sql", "params", "table")

    def __init__(self, sql: str, params: list[Any], table: str) -> None:
        """Store the raw SQL fragment, its positional params, and the table it scopes."""
        self.sql = sql
        self.params = list(params)
        self.table = table

    def __and__(self, other: Query) -> Query:
        """Combine two query fragments with SQL ``AND``."""
        return Query(f"({self.sql}) AND ({other.sql})", self.params + other.params, self.table)

    def __or__(self, other: Query) -> Query:
        """Combine two query fragments with SQL ``OR``."""
        return Query(f"({self.sql}) OR ({other.sql})", self.params + other.params, self.table)

    def render(self, start: int = 1) -> tuple[str, int]:
        r"""Render ``\0`` placeholders as ``$start``, ``$start+1``, ... in order."""
        idx = start
        pieces = self.sql.split("\0")
        rendered = pieces[0]
        for piece in pieces[1:]:
            rendered += f"${idx}{piece}"
            idx += 1
        return rendered, idx

    def __repr__(self) -> str:
        """Return a debug-friendly representation."""
        return f"Query({self.sql!r}, params={self.params!r})"


class FieldProxy:
    """One ``table.column`` reference -- matches ``penguin_dal.field_proxy.FieldProxy``.

    ``like``/``ilike``/``contains``/``startswith``/``endswith``/``belongs`` raise
    ``NotImplementedError`` naming the construct (D21's rule: an explicit gap,
    never a silent mis-execution) -- no first-party bundle call site uses them
    as of this SDK's initial cut; add real lowering here before any bundle
    needs one.
    """

    __slots__ = ("_table", "_name")

    def __init__(self, table: str, name: str) -> None:
        """Bind this proxy to one ``table.column`` reference."""
        self._table = table
        self._name = name

    def __eq__(self, other: object) -> Query:  # type: ignore[override]
        """Build ``col = $n`` (or ``IS NULL`` for ``None``)."""
        if other is None:
            return Query(f"{self._table}.{self._name} IS NULL", [], self._table)
        return Query(f"{self._table}.{self._name} = \0", [other], self._table)

    def __ne__(self, other: object) -> Query:  # type: ignore[override]
        """Build ``col != $n`` (or ``IS NOT NULL`` for ``None``)."""
        if other is None:
            return Query(f"{self._table}.{self._name} IS NOT NULL", [], self._table)
        return Query(f"{self._table}.{self._name} != \0", [other], self._table)

    def __gt__(self, other: Any) -> Query:
        """Build ``col > $n``."""
        return Query(f"{self._table}.{self._name} > \0", [other], self._table)

    def __lt__(self, other: Any) -> Query:
        """Build ``col < $n``."""
        return Query(f"{self._table}.{self._name} < \0", [other], self._table)

    def __ge__(self, other: Any) -> Query:
        """Build ``col >= $n``."""
        return Query(f"{self._table}.{self._name} >= \0", [other], self._table)

    def __le__(self, other: Any) -> Query:
        """Build ``col <= $n``."""
        return Query(f"{self._table}.{self._name} <= \0", [other], self._table)

    def like(self, pattern: str) -> Query:
        """Not implemented -- see class docstring."""
        raise NotImplementedError("FieldProxy.like() is not implemented in the waddle-sdk facade")

    def ilike(self, pattern: str) -> Query:
        """Not implemented -- see class docstring."""
        raise NotImplementedError("FieldProxy.ilike() is not implemented in the waddle-sdk facade")

    def contains(self, value: str) -> Query:
        """Not implemented -- see class docstring."""
        raise NotImplementedError(
            "FieldProxy.contains() is not implemented in the waddle-sdk facade"
        )

    def startswith(self, value: str) -> Query:
        """Not implemented -- see class docstring."""
        raise NotImplementedError(
            "FieldProxy.startswith() is not implemented in the waddle-sdk facade"
        )

    def endswith(self, value: str) -> Query:
        """Not implemented -- see class docstring."""
        raise NotImplementedError(
            "FieldProxy.endswith() is not implemented in the waddle-sdk facade"
        )

    def belongs(self, values: Any) -> Query:
        """Not implemented -- see class docstring."""
        raise NotImplementedError(
            "FieldProxy.belongs() is not implemented in the waddle-sdk facade"
        )

    def __hash__(self) -> int:  # needed because __eq__ is overridden above
        """Hash by (table, column) identity."""
        return hash((self._table, self._name))

    def __repr__(self) -> str:
        """Return a debug-friendly representation."""
        return f"FieldProxy({self._table}.{self._name})"


# `Field` is the same type as `FieldProxy` in this facade -- the real
# penguin_dal exposes both names (`Field` from `.field`, `FieldProxy` from
# `.field_proxy`) as historically-distinct but call-compatible types; a
# single class satisfies both import names here.
Field = FieldProxy


class Row:
    """One result row -- dict AND attribute access, matching ``penguin_dal.query.Row``."""

    def __init__(self, data: dict[str, Any]) -> None:
        """Wrap one row's column-name -> value mapping."""
        self._data = data

    def __getitem__(self, key: str) -> Any:
        """Return ``self._data[key]``."""
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        """Return whether ``key`` is a column in this row."""
        return key in self._data

    def __iter__(self) -> Any:
        """Iterate over column names."""
        return iter(self._data)

    def __len__(self) -> int:
        """Return the number of columns."""
        return len(self._data)

    def __eq__(self, other: object) -> bool:
        """Compare by underlying data mapping."""
        return isinstance(other, Row) and self._data == other._data

    def __getattr__(self, name: str) -> Any:
        """Return ``self._data[name]`` for any non-dunder attribute."""
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def keys(self) -> list[str]:
        """Return the column names."""
        return list(self._data.keys())

    def values(self) -> list[Any]:
        """Return the column values."""
        return list(self._data.values())

    def items(self) -> list[tuple[str, Any]]:
        """Return the (column, value) pairs."""
        return list(self._data.items())

    def as_dict(self) -> dict[str, Any]:
        """Return a plain ``dict`` copy of this row."""
        return dict(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        """Return ``self._data.get(key, default)``."""
        return self._data.get(key, default)

    def __repr__(self) -> str:
        """Return a debug-friendly representation."""
        return f"Row({self._data!r})"


class Rows:
    """A result set -- matches ``penguin_dal.query.Rows``.

    Truthy/iterable/indexable, with ``.first()``/``.last()``/``.as_list()``.
    """

    def __init__(self, rows: list[Row]) -> None:
        """Wrap a list of already-materialized :class:`Row` objects."""
        self.rows = rows

    def first(self) -> Row | None:
        """Return the first row, or ``None`` if empty."""
        return self.rows[0] if self.rows else None

    def last(self) -> Row | None:
        """Return the last row, or ``None`` if empty."""
        return self.rows[-1] if self.rows else None

    def as_list(self) -> list[dict[str, Any]]:
        """Return every row as a plain ``dict``."""
        return [r.as_dict() for r in self.rows]

    def __iter__(self) -> Any:
        """Iterate over rows."""
        return iter(self.rows)

    def __len__(self) -> int:
        """Return the number of rows."""
        return len(self.rows)

    def __getitem__(self, index: int) -> Row:
        """Return the row at ``index``."""
        return self.rows[index]

    def __bool__(self) -> bool:
        """Return whether any rows are present."""
        return bool(self.rows)

    def __repr__(self) -> str:
        """Return a debug-friendly representation."""
        return f"Rows({self.rows!r})"


class AsyncQuerySet:
    """Created by ``AsyncDB.__call__(query)``.

    Matches ``penguin_dal.query.AsyncQuerySet``'s public methods exactly
    (``select``/``update``/``delete``/``count``/``exists``, all ``async def``).
    ``orderby``/``limitby`` are accepted for signature compatibility and raise
    ``NotImplementedError`` if actually supplied.
    """

    def __init__(self, table_name: str, query: Query | None) -> None:
        """Scope this query set to ``table_name``, optionally filtered by ``query``."""
        self._table_name = table_name
        self._query = query

    async def select(
        self, *columns: FieldProxy, orderby: Any = None, limitby: tuple[int, int] | None = None
    ) -> Rows:
        """Run a ``SELECT`` and return the matching rows."""
        if orderby is not None or limitby is not None:
            raise NotImplementedError(
                "AsyncQuerySet.select(orderby=..., limitby=...) is not implemented "
                "in the waddle-sdk facade"
            )
        select_list = (
            "*" if not columns else ", ".join(f"{self._table_name}.{c._name}" for c in columns)
        )
        if self._query is not None:
            where_sql, _ = self._query.render(1)
            sql = f"SELECT {select_list} FROM {self._table_name} WHERE {where_sql}"
            params = self._query.params
        else:
            sql = f"SELECT {select_list} FROM {self._table_name}"
            params = []
        rows, _ = _cross(sql, params)
        return Rows([Row(r) for r in rows])

    async def update(self, **kwargs: Any) -> int:
        """Run an ``UPDATE`` and return the number of rows affected."""
        set_cols = list(kwargs.keys())
        set_clause = ", ".join(f"{col} = ${i + 1}" for i, col in enumerate(set_cols))
        set_params = [kwargs[c] for c in set_cols]
        if self._query is not None:
            where_sql, _ = self._query.render(len(set_cols) + 1)
            sql = f"UPDATE {self._table_name} SET {set_clause} WHERE {where_sql}"
            params = set_params + self._query.params
        else:
            sql = f"UPDATE {self._table_name} SET {set_clause}"
            params = set_params
        _, rows_affected = _cross(sql, params)
        return rows_affected

    async def delete(self) -> int:
        """Run a ``DELETE`` and return the number of rows affected."""
        if self._query is not None:
            where_sql, _ = self._query.render(1)
            sql = f"DELETE FROM {self._table_name} WHERE {where_sql}"
            params = self._query.params
        else:
            sql = f"DELETE FROM {self._table_name}"
            params = []
        _, rows_affected = _cross(sql, params)
        return rows_affected

    async def count(self) -> int:
        """Return the number of matching rows."""
        if self._query is not None:
            where_sql, _ = self._query.render(1)
            sql = f"SELECT COUNT(*) as count FROM {self._table_name} WHERE {where_sql}"
            params = self._query.params
        else:
            sql = f"SELECT COUNT(*) as count FROM {self._table_name}"
            params = []
        rows, _ = _cross(sql, params)
        return int(rows[0]["count"]) if rows else 0

    async def exists(self) -> bool:
        """Return whether any row matches."""
        if self._query is not None:
            where_sql, _ = self._query.render(1)
            sql = f"SELECT 1 as exists FROM {self._table_name} WHERE {where_sql}"
            params = self._query.params
        else:
            sql = f"SELECT 1 as exists FROM {self._table_name}"
            params = []
        rows, _ = _cross(sql, params)
        return len(rows) > 0


class TableProxy:
    """``db.command_aliases`` -- matches ``penguin_dal.table_proxy.TableProxy``.

    **Documented, necessary deviation from ``penguin_dal``:** the real
    ``TableProxy.__getattr__`` validates column names against live-reflected
    SQLAlchemy metadata; this facade has no metadata to reflect (there is no
    live connection inside the sandbox -- the WIT ``db`` import *is* the
    connection), so ``__getattr__`` returns a ``FieldProxy`` for any attribute
    name unconditionally. Column/table-name validation happens where it
    always has to happen in this design: the stage's SQL parser and Postgres
    itself.
    """

    def __init__(self, name: str) -> None:
        """Bind this proxy to one table name."""
        self._name = name

    @property
    def table_name(self) -> str:
        """Return the table name."""
        return self._name

    def __getattr__(self, name: str) -> FieldProxy:
        """Return a ``FieldProxy`` for any non-dunder attribute name."""
        if name.startswith("_"):
            raise AttributeError(name)
        return FieldProxy(self._name, name)

    def __getitem__(self, pk: Any) -> Row | None:
        """PK lookup -- ``db.table[42]``.

        Synchronous, matching the real ``TableProxy.__getitem__``'s return
        type -- this facade's ``_cross()`` is itself a synchronous WIT host
        call (D21: "single-threaded and synchronous underneath"), so no event
        loop is needed here at all, unlike the real implementation's
        ``run_until_complete`` (which exists there only because its
        underlying engine call genuinely is async I/O).
        """
        rows, _ = _cross(f"SELECT * FROM {self._name} WHERE {self._name}.id = $1", [pk])
        return Row(rows[0]) if rows else None

    def insert(self, **kwargs: Any) -> Any:
        """Not implemented (sync) -- use :meth:`async_insert`."""
        raise NotImplementedError(
            "TableProxy.insert() (sync) is not implemented in the waddle-sdk facade "
            "-- use async_insert()"
        )

    async def async_insert(self, **kwargs: Any) -> Any:
        """Insert one row and return its ``id`` (appends ``RETURNING id``)."""
        cols = list(kwargs.keys())
        col_list = ", ".join(cols)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
        sql = f"INSERT INTO {self._name} ({col_list}) VALUES ({placeholders}) RETURNING id"
        params = [kwargs[c] for c in cols]
        rows, _ = _cross(sql, params)
        return rows[0]["id"] if rows else None

    def bulk_insert(self, rows: list[dict[str, Any]]) -> None:
        """Not implemented (sync) -- use :meth:`async_bulk_insert`."""
        raise NotImplementedError(
            "TableProxy.bulk_insert() (sync) is not implemented in the waddle-sdk facade "
            "-- use async_bulk_insert()"
        )

    async def async_bulk_insert(self, rows: list[dict[str, Any]]) -> None:
        """Insert multiple rows, one ``async_insert()`` call each."""
        for row in rows:
            await self.async_insert(**row)

    def __repr__(self) -> str:
        """Return a debug-friendly representation."""
        return f"TableProxy({self._name})"


class AsyncDB:
    """``get_bundle_dal()``'s return value -- matches ``penguin_dal.db.AsyncDB``.

    ``__getattr__`` -> ``TableProxy``, ``__call__(query)`` -> ``AsyncQuerySet``,
    plus the raw ``execute()`` escape hatch.
    """

    def __getattr__(self, name: str) -> TableProxy:
        """Return a ``TableProxy`` for any non-dunder attribute name."""
        if name.startswith("_"):
            raise AttributeError(name)
        return TableProxy(name)

    def __call__(self, query: Query | None = None) -> AsyncQuerySet:
        """Return an ``AsyncQuerySet`` scoped to ``query``."""
        table_name = query.table if query is not None else None
        if table_name is None:
            raise ValidationError(
                "AsyncDB.__call__(query) requires a query built from a table's own FieldProxy"
            )
        return AsyncQuerySet(table_name, query)

    async def execute(
        self, statement: str, params: list[Any] | None = None
    ) -> list[dict[str, Any]]:
        """Run raw SQL (``$1``/``$2``/... already in ``statement``) and return the rows."""
        rows, _ = _cross(statement, list(params) if params else [])
        return rows

    @property
    def engine(self) -> Any:
        """Not implemented.

        Real ``penguin_dal.AsyncDB.engine`` exposes a live SQLAlchemy async
        engine for raw connection use (``flask_core.bundle_runtime.raw_sql_rows``/
        ``raw_sql_write`` build on exactly this). There is no live engine
        inside the sandbox -- the WIT ``db`` import *is* the connection, one
        parameterized statement at a time -- so this construct cannot be
        lowered and raises explicitly (D21) rather than returning something
        that silently mis-executes.
        """
        raise NotImplementedError(
            "AsyncDB.engine is not implemented in the waddle-sdk facade -- there is no "
            "live SQLAlchemy engine inside the sandbox; use the query builder or "
            "AsyncDB.execute() instead of a raw engine connection"
        )

    async def commit(self) -> None:
        """No-op -- every statement already committed host-side, per statement."""
        return None

    async def close(self) -> None:
        """No-op -- there is no client-held connection to close."""
        return None


DB = AsyncDB
# `penguin_dal.db.DB` is the sync variant upstream; unified here for the same
# "no client-held connection" reason `commit`/`close` are no-ops.


class DatabaseManager:
    """Not implemented.

    Real ``penguin_dal.DatabaseManager`` opens primary/replica connections
    from two database URLs and routes reads/writes between them -- there is
    no database URL a WASM guest can connect to (the WIT ``db`` import is the
    only connection, and read/write routing is a host-side, not guest-side,
    concern). Raises explicitly on construction rather than being silently
    unusable.
    """

    def __init__(self, write_url: str, read_url: str | None = None, **kwargs: Any) -> None:
        """Always raise -- see class docstring."""
        raise NotImplementedError(
            "DatabaseManager is not implemented in the waddle-sdk facade -- there is no "
            "database URL to connect to inside the sandbox; read/write splitting is a "
            "host-side concern"
        )


def create_dal() -> AsyncDB:
    """Matches ``penguin_dal.factory.create_dal`` by name and return type only.

    Takes no arguments, since there is no database URL to connect to inside
    the sandbox (the WIT ``db`` import *is* the connection). See this
    module's docstring for why this is a necessary, documented deviation.
    """
    return AsyncDB()
