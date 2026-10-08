"""Fake `wit_world.imports.db` shapes + an in-memory backing store, for this bundle's own tests.

Reproduces the exact class names `componentize-py bindings` generates for the committed
`wit/waddle-bundle/stage.wit` `db` interface (`Value_NullValue`, `ColumnValue`, `OrderBy_Column`,
`Error_NotFound`, etc.) -- `waddle_sdk.db`'s own facade classifies errors and unwraps values by
matching these exact names via `type(x).__name__` (see its module docstring), so a fake that
didn't reproduce them exactly would silently misclassify instead of failing a test assertion.

This mirrors `bundles/python/rank/tests/wit_fake_db.py`'s own identical reproduction (reproduced
locally -- smaller and bundle-specific -- rather than imported, same reasoning that module's own
docstring gives), extended with a `delete` op (`rank` never deletes a row; this bundle's own
`!quote remove` does).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Value_NullValue:
    """Matches the generated `db.value` `null-value` case."""


@dataclass
class Value_BoolValue:
    """Matches the generated `db.value` `bool-value` case."""

    value: bool


@dataclass
class Value_IntValue:
    """Matches the generated `db.value` `int-value` case."""

    value: int


@dataclass
class Value_FloatValue:
    """Matches the generated `db.value` `float-value` case."""

    value: float


@dataclass
class Value_TextValue:
    """Matches the generated `db.value` `text-value` case."""

    value: str


@dataclass
class Value_BytesValue:
    """Matches the generated `db.value` `bytes-value` case."""

    value: bytes


@dataclass
class ColumnValue:
    """One `(column, value)` pair, matching the generated `db.column-value` record."""

    column: str
    value: Any


@dataclass
class OrderColumn:
    """Matches the generated `db.order-column` record."""

    name: str
    descending: bool = False


@dataclass
class OrderBy_Column:
    """Matches the generated `db.order-by` `column` case."""

    value: OrderColumn


@dataclass
class OrderBy_Random:
    """Matches the generated `db.order-by` `random` case."""


@dataclass
class Row:
    """Matches the generated `db.row` record."""

    row_id: str
    version: int
    columns: list[ColumnValue] = field(default_factory=list)


class Error_NotFound:
    """Matches the generated `db.error` `not-found` case (no payload)."""


class Error_Conflict:
    """Matches the generated `db.error` `conflict` case."""

    def __init__(self, value: str = "") -> None:
        """Store the conflict detail string."""
        self.value = value


class Error_Backend:
    """Matches the generated `db.error` `backend` case."""

    def __init__(self, value: str = "") -> None:
        """Store the backend error detail string."""
        self.value = value


class WitDbError(Exception):
    """Stand-in for the generated `Err` wrapper -- `.value` holds the error union member."""

    def __init__(self, value: Any) -> None:
        """Store the wrapped `db.error` case."""
        self.value = value


def wrap(value: Any) -> Any:
    """Wrap a plain Python scalar into a fake WIT `db.value` union member."""
    if value is None:
        return Value_NullValue()
    if isinstance(value, bool):
        return Value_BoolValue(value)
    if isinstance(value, int):
        return Value_IntValue(value)
    if isinstance(value, float):
        return Value_FloatValue(value)
    if isinstance(value, bytes | bytearray):
        return Value_BytesValue(bytes(value))
    return Value_TextValue(str(value))


def unwrap(value: Any) -> Any:
    """Unwrap a fake WIT `db.value` union member back to a plain Python scalar."""
    if isinstance(value, Value_NullValue):
        return None
    if isinstance(value, Value_BoolValue | Value_IntValue | Value_FloatValue | Value_TextValue):
        return value.value
    if isinstance(value, Value_BytesValue):
        return bytes(value.value)
    raise AssertionError(f"unrecognized fake Value case {type(value).__name__!r}")


class FakeDb:
    """Records calls; answers from one in-memory table keyed by a fake `row_id`.

    Structured ops only (insert/get/query/update/delete), matching the real WIT `db` interface
    -- see module docstring.
    """

    def __init__(self) -> None:
        """Start with an empty table and no forced errors."""
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.rows: dict[str, dict[str, Any]] = {}
        self.versions: dict[str, int] = {}
        self._id_counter = itertools.count(1)
        self.raise_on: dict[str, Exception] = {}

    def _next_row_id(self) -> str:
        return f"00000000-0000-0000-0000-{next(self._id_counter):012d}"

    def _to_row(self, row_id: str) -> Row:
        columns = [ColumnValue(column=k, value=wrap(v)) for k, v in self.rows[row_id].items()]
        return Row(row_id=row_id, version=self.versions[row_id], columns=columns)

    def insert(self, column_values: list[ColumnValue]) -> Row:
        """Fake `db.insert(column-values) -> row`."""
        self.calls.append(("insert", (column_values,)))
        if "insert" in self.raise_on:
            raise self.raise_on["insert"]
        row_id = self._next_row_id()
        self.rows[row_id] = {cv.column: unwrap(cv.value) for cv in column_values}
        self.versions[row_id] = 1
        return self._to_row(row_id)

    def get(self, row_id: str) -> Row:
        """Fake `db.get(row-id) -> row`, raising `Error_NotFound` if absent."""
        self.calls.append(("get", (row_id,)))
        if "get" in self.raise_on:
            raise self.raise_on["get"]
        if row_id not in self.rows:
            raise WitDbError(Error_NotFound())
        return self._to_row(row_id)

    def query(self, limit: int, offset: int, order_by: Any = None) -> list[Row]:
        """Fake `db.query(limit, offset, order-by) -> list<row>` -- see the real facade's doc."""
        self.calls.append(("query", (limit, offset, order_by)))
        if "query" in self.raise_on:
            raise self.raise_on["query"]
        ids = sorted(self.rows)
        if isinstance(order_by, OrderBy_Column):
            col = order_by.value
            ids = sorted(ids, key=lambda i: self.rows[i].get(col.name), reverse=col.descending)
        elif isinstance(order_by, OrderBy_Random):
            ids = list(reversed(ids))
        page = ids[offset : offset + limit]
        return [self._to_row(i) for i in page]

    def update(self, row_id: str, expected_version: int, column_values: list[ColumnValue]) -> Row:
        """Fake `db.update(row-id, expected-version, column-values) -> row`."""
        self.calls.append(("update", (row_id, expected_version, column_values)))
        if "update" in self.raise_on:
            raise self.raise_on["update"]
        if row_id not in self.rows:
            raise WitDbError(Error_NotFound())
        if self.versions[row_id] != expected_version:
            raise WitDbError(Error_Conflict("version mismatch"))
        self.rows[row_id].update({cv.column: unwrap(cv.value) for cv in column_values})
        self.versions[row_id] += 1
        return self._to_row(row_id)

    def delete(self, row_id: str, expected_version: int) -> None:
        """Fake `db.delete(row-id, expected-version)` -- a real delete, no return value."""
        self.calls.append(("delete", (row_id, expected_version)))
        if "delete" in self.raise_on:
            raise self.raise_on["delete"]
        if row_id not in self.rows:
            raise WitDbError(Error_NotFound())
        if self.versions[row_id] != expected_version:
            raise WitDbError(Error_Conflict("version mismatch"))
        del self.rows[row_id]
        del self.versions[row_id]
