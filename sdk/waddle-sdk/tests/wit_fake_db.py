"""A fake `wit_world.imports.db` module for waddle-sdk's own pytest suite.

Records every `execute(statement, params)` call and answers from a canned
table, keyed by a substring of the statement. Every dataclass used here is
`wit_shapes`'s exact reproduction of componentize-py's real generated
bindings for `wit/waddle-bundle/stage.wit` -- this is a faithful test double
of the host boundary, not a simplified stand-in.
"""

from __future__ import annotations

from typing import Any

import wit_shapes


def _wrap(value: Any) -> Any:
    """Wrap a plain Python scalar into the fake WIT `db.Value` union (mirrors `_to_wit_value`)."""
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
    """Unwrap one fake WIT `db.Value` union member back to a Python scalar."""
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
    """Records calls; answers canned rows/row-counts keyed by a statement substring."""

    def __init__(self) -> None:
        """Start with no recorded calls and no canned responses."""
        self.calls: list[tuple[str, list[Any]]] = []
        self.canned_rows: dict[str, list[dict[str, Any]]] = {}
        self.canned_rows_affected: dict[str, int] = {}
        self.raise_on: dict[str, Any] = {}

    def execute(self, statement: str, params: list[Any]) -> wit_shapes.DbRows:
        """Fake implementation of the generated `db.execute(statement, params) -> Rows`."""
        py_params = [_unwrap(p) for p in params]
        self.calls.append((statement, py_params))
        for key, error in self.raise_on.items():
            if key in statement:
                raise wit_shapes.Err(error)
        for key, rows in self.canned_rows.items():
            if key in statement:
                columns = list(rows[0].keys()) if rows else []
                wit_rows = [[_wrap(row[c]) for c in columns] for row in rows]
                return wit_shapes.DbRows(columns=columns, rows=wit_rows, rows_affected=len(rows))
        rows_affected = 0
        for key, count in self.canned_rows_affected.items():
            if key in statement:
                rows_affected = count
                break
        return wit_shapes.DbRows(columns=[], rows=[], rows_affected=rows_affected)


def install(monkeypatch: Any) -> FakeWitDb:
    """Install a fresh `FakeWitDb` as `sys.modules["wit_world"].imports.db` and return it.

    `db.py`'s `_to_wit_value` reaches `db_mod.Value_NullValue`/etc as
    attributes of the same module object `execute` lives on -- exactly like
    the real generated `wit_world.imports.db` module, which defines both the
    dataclasses and the function at module scope. The fake namespace below
    mirrors that: `execute` is bound to the stateful `FakeWitDb` instance,
    and every `Value_*`/`Rows` name is the same class `wit_fake_db._unwrap`
    already knows how to read.
    """
    import sys
    import types

    fake = FakeWitDb()
    db_mod = types.SimpleNamespace(
        execute=fake.execute,
        Value_NullValue=wit_shapes.Value_NullValue,
        Value_BoolValue=wit_shapes.Value_BoolValue,
        Value_IntValue=wit_shapes.Value_IntValue,
        Value_FloatValue=wit_shapes.Value_FloatValue,
        Value_TextValue=wit_shapes.Value_TextValue,
        Value_BytesValue=wit_shapes.Value_BytesValue,
        Rows=wit_shapes.DbRows,
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(db=db_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return fake
