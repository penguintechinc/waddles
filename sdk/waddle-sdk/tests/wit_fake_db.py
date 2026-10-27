"""A fake `wit_world.imports.db` module for waddle-sdk's own pytest suite.

Structured ops (insert/get/query/update/delete) against one in-memory table,
keyed by a UUID-shaped `row_id` this fake assigns -- matches the real WIT
`db` interface (`wit/waddle-bundle/stage.wit`) shape, not the retired raw-SQL
`execute` one. Every dataclass used here is `wit_shapes`'s exact reproduction
of componentize-py's real generated bindings -- this is a faithful test
double of the host boundary, not a simplified stand-in.
"""

from __future__ import annotations

import itertools
from typing import Any

import wit_shapes


def _wrap(value: Any) -> Any:
    """Wrap a plain Python scalar into the fake WIT `db.value` union (mirrors `_to_wit_value`)."""
    if value is None:
        return wit_shapes.Value_NullValue()
    if isinstance(value, bool):
        return wit_shapes.Value_BoolValue(value)
    if isinstance(value, int):
        return wit_shapes.Value_IntValue(value)
    if isinstance(value, float):
        return wit_shapes.Value_FloatValue(value)
    if isinstance(value, bytes | bytearray):
        return wit_shapes.Value_BytesValue(bytes(value))
    return wit_shapes.Value_TextValue(str(value))


def _unwrap(value: Any) -> Any:
    """Unwrap one fake WIT `db.value` union member back to a Python scalar."""
    if isinstance(value, wit_shapes.Value_NullValue):
        return None
    if isinstance(
        value,
        wit_shapes.Value_BoolValue
        | wit_shapes.Value_IntValue
        | wit_shapes.Value_FloatValue
        | wit_shapes.Value_TextValue,
    ):
        return value.value
    if isinstance(value, wit_shapes.Value_BytesValue):
        return bytes(value.value)
    raise AssertionError(f"wit_fake_db: unrecognized fake Value case {type(value).__name__!r}")


class FakeWitDb:
    """Records calls; answers from one in-memory table keyed by a fake `row_id`."""

    def __init__(self) -> None:
        """Start with an empty table and no forced errors."""
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._rows: dict[str, dict[str, Any]] = {}
        self._versions: dict[str, int] = {}
        self._id_counter = itertools.count(1)
        self.raise_on: dict[str, Exception] = {}

    def _next_row_id(self) -> str:
        return f"00000000-0000-0000-0000-{next(self._id_counter):012d}"

    def _maybe_raise(self, op: str) -> None:
        if op in self.raise_on:
            raise self.raise_on[op]

    def insert(self, column_values: list[Any]) -> wit_shapes.DbRow:
        """Fake `db.insert(column-values) -> row`."""
        self.calls.append(("insert", (column_values,)))
        self._maybe_raise("insert")
        row_id = self._next_row_id()
        data = {cv.column: _unwrap(cv.value) for cv in column_values}
        self._rows[row_id] = data
        self._versions[row_id] = 1
        return self._to_row(row_id)

    def get(self, row_id: str) -> wit_shapes.DbRow:
        """Fake `db.get(row-id) -> row`, raising `Error_NotFound` if absent."""
        self.calls.append(("get", (row_id,)))
        self._maybe_raise("get")
        if row_id not in self._rows:
            raise wit_shapes.Err(wit_shapes.DbError_NotFound())
        return self._to_row(row_id)

    def query(self, limit: int, offset: int, order_by: Any = None) -> list[wit_shapes.DbRow]:
        """Fake `db.query(limit, offset, order-by) -> list<row>`.

        `order_by` is the generated `db.order-by` variant
        (`OrderBy_Column(OrderColumn(name, descending))` / `OrderBy_Random()`,
        or `None`) -- mirrors the real backend's own default-to-`row_id`-
        ascending behavior, plus a declared-column sort and a seeded
        "random" shuffle (deterministic here, since this fake has no real
        randomness to prove against -- only that the op reorders at all).
        """
        self.calls.append(("query", (limit, offset, order_by)))
        self._maybe_raise("query")
        ids = sorted(self._rows)
        if order_by is not None:
            type_name = type(order_by).__name__
            if type_name == "OrderBy_Column":
                col = order_by.value
                ids = sorted(ids, key=lambda i: self._rows[i].get(col.name), reverse=col.descending)
            elif type_name == "OrderBy_Random":
                ids = list(reversed(ids))
        page = ids[offset : offset + limit]
        return [self._to_row(i) for i in page]

    def update(
        self, row_id: str, expected_version: int, column_values: list[Any]
    ) -> wit_shapes.DbRow:
        """Fake `db.update(row-id, expected-version, column-values) -> row`."""
        self.calls.append(("update", (row_id, expected_version, column_values)))
        self._maybe_raise("update")
        if row_id not in self._rows:
            raise wit_shapes.Err(wit_shapes.DbError_NotFound())
        if self._versions[row_id] != expected_version:
            raise wit_shapes.Err(wit_shapes.DbError_Conflict("version mismatch"))
        self._rows[row_id].update({cv.column: _unwrap(cv.value) for cv in column_values})
        self._versions[row_id] += 1
        return self._to_row(row_id)

    def delete(self, row_id: str, expected_version: int) -> None:
        """Fake `db.delete(row-id, expected-version)`."""
        self.calls.append(("delete", (row_id, expected_version)))
        self._maybe_raise("delete")
        if row_id not in self._rows:
            raise wit_shapes.Err(wit_shapes.DbError_NotFound())
        if self._versions[row_id] != expected_version:
            raise wit_shapes.Err(wit_shapes.DbError_Conflict("version mismatch"))
        del self._rows[row_id]
        del self._versions[row_id]

    def _to_row(self, row_id: str) -> wit_shapes.DbRow:
        columns = [
            wit_shapes.ColumnValue(column=k, value=_wrap(v)) for k, v in self._rows[row_id].items()
        ]
        return wit_shapes.DbRow(row_id=row_id, version=self._versions[row_id], columns=columns)


def install(monkeypatch: Any) -> FakeWitDb:
    """Install a fresh `FakeWitDb` as `sys.modules["wit_world"].imports.db` and return it."""
    import sys
    import types

    fake = FakeWitDb()
    db_mod = types.SimpleNamespace(
        insert=fake.insert,
        get=fake.get,
        query=fake.query,
        update=fake.update,
        delete=fake.delete,
        ColumnValue=wit_shapes.ColumnValue,
        OrderColumn=wit_shapes.OrderColumn,
        OrderBy_Column=wit_shapes.OrderBy_Column,
        OrderBy_Random=wit_shapes.OrderBy_Random,
        Value_NullValue=wit_shapes.Value_NullValue,
        Value_BoolValue=wit_shapes.Value_BoolValue,
        Value_IntValue=wit_shapes.Value_IntValue,
        Value_FloatValue=wit_shapes.Value_FloatValue,
        Value_TextValue=wit_shapes.Value_TextValue,
        Value_BytesValue=wit_shapes.Value_BytesValue,
        Row=wit_shapes.DbRow,
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(db=db_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return fake
