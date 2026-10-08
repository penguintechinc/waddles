"""A minimal, self-contained fake `wit_world.imports.db` for this bundle's own pytest suite.

Deliberately NOT a copy of `sdk/waddle-sdk/tests/wit_fake_db.py` (that fixture only
records calls and answers *canned* rows -- fine for SDK-internal SQL-generation tests,
not expressive enough to exercise this bundle's own multi-step create/vote/close/list/
view flows against realistic, mutating, in-memory table state). This is a tiny
relational engine instead: real `INSERT ... RETURNING`/`SELECT ... WHERE`/`UPDATE ... SET
... WHERE` semantics over Python dicts, driven by regex extraction of `waddle_sdk.db`'s
own generated SQL shapes (`$N`-indexed params; `AND`-only WHERE clauses -- `app.py` never
builds an `OR`, matching every query this bundle actually issues).

The `Value_*`/`Rows`/`Err` shapes below are this test file's own minimal reproduction of
the real `componentize-py`-generated bindings (same case names `waddle_sdk/db.py`
dispatches on by `type(value).__name__`), not an import of `wit_shapes.py` (that file
lives under `sdk/waddle-sdk/tests/`, outside this bundle's own test `sys.path`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_LEAF_EQ_RE = re.compile(r"(\w+)\.(\w+) = \$(\d+)")
_LEAF_NULL_RE = re.compile(r"(\w+)\.(\w+) IS NULL")
_LEAF_NOT_NULL_RE = re.compile(r"(\w+)\.(\w+) IS NOT NULL")
_SET_RE = re.compile(r"(\w+) = \$(\d+)")
_INSERT_RE = re.compile(
    r"^INSERT INTO (\w+) \(([^)]*)\) VALUES \(([^)]*)\) RETURNING (\w+)$"
)
_SELECT_RE = re.compile(r"^SELECT \* FROM (\w+)(?: WHERE (.*))?$")
_UPDATE_RE = re.compile(r"^UPDATE (\w+) SET (.*?) WHERE (.*)$")


@dataclass
class Value_NullValue:  # noqa: N801 - mirrors the real generated case name
    """Fake WIT `db.value` null case."""


@dataclass
class Value_BoolValue:  # noqa: N801
    """Fake WIT `db.value` bool case."""

    value: bool


@dataclass
class Value_IntValue:  # noqa: N801
    """Fake WIT `db.value` int case."""

    value: int


@dataclass
class Value_FloatValue:  # noqa: N801
    """Fake WIT `db.value` float case."""

    value: float


@dataclass
class Value_TextValue:  # noqa: N801
    """Fake WIT `db.value` text case."""

    value: str


@dataclass
class Value_BytesValue:  # noqa: N801
    """Fake WIT `db.value` bytes case."""

    value: bytes


@dataclass
class Rows:
    """Fake WIT `db.rows` record."""

    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    rows_affected: int = 0


class Err(Exception):
    """Fake generated `Err` -- `.value` holds the error detail, mirroring the real shape."""

    def __init__(self, value: Any) -> None:
        """Store `value` (what `db.py`'s `getattr(exc, "value", exc)` reads)."""
        self.value = value
        super().__init__(str(value))


def _wrap(value: Any) -> Any:
    """Wrap a plain Python scalar into the fake `db.Value` union."""
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


def _unwrap(value: Any) -> Any:
    """Unwrap one fake `db.Value` union member back to a Python scalar."""
    if isinstance(value, Value_NullValue):
        return None
    if isinstance(value, Value_BoolValue | Value_IntValue | Value_FloatValue | Value_TextValue):
        return value.value
    if isinstance(value, Value_BytesValue):
        return bytes(value.value)
    raise AssertionError(f"fake_wit_db: unrecognized fake Value case {type(value).__name__!r}")


class FakeWitDb:
    """In-memory tables + a tiny SQL-shape-aware `execute()`, matching `waddle_sdk.db`'s output.

    `raise_on`: a substring -> exception mapping; any `execute()` call whose statement
    contains the substring raises that exception instead of touching table state (for
    fail-loud tests).
    """

    def __init__(self) -> None:
        """Start with empty tables, per-table id counters, and a monotonic insert clock."""
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self._next_id: dict[str, int] = {}
        self._clock = 0
        self.calls: list[tuple[str, list[Any]]] = []
        self.raise_on: dict[str, Exception] = {}

    def seed(self, table: str, rows: list[dict[str, Any]]) -> None:
        """Pre-populate `table` with `rows`, advancing that table's id counter past them."""
        self.tables.setdefault(table, []).extend(rows)
        max_id = max((r.get("id", 0) for r in rows), default=0)
        self._next_id[table] = max(self._next_id.get(table, 0), max_id)

    def _row_matches(self, table: str, row: dict[str, Any], where: str, params: list[Any]) -> bool:
        """Every `AND`-joined leaf predicate in `where` must hold -- this bundle never emits `OR`."""
        for tbl, col, idx in _LEAF_EQ_RE.findall(where):
            if tbl == table and row.get(col) != params[int(idx) - 1]:
                return False
        for tbl, col in _LEAF_NULL_RE.findall(where):
            if tbl == table and row.get(col) is not None:
                return False
        for tbl, col in _LEAF_NOT_NULL_RE.findall(where):
            if tbl == table and row.get(col) is None:
                return False
        return True

    def execute(self, statement: str, params: list[Any]) -> Rows:
        """Fake implementation of the generated `db.execute(statement, params) -> Rows`."""
        py_params = [_unwrap(p) for p in params]
        self.calls.append((statement, py_params))
        for needle, exc in self.raise_on.items():
            if needle in statement:
                raise exc

        insert_match = _INSERT_RE.match(statement)
        if insert_match:
            return self._execute_insert(insert_match, py_params)

        update_match = _UPDATE_RE.match(statement)
        if update_match:
            return self._execute_update(update_match, py_params)

        select_match = _SELECT_RE.match(statement)
        if select_match:
            return self._execute_select(select_match, py_params)

        raise AssertionError(f"fake_wit_db: unrecognized statement shape: {statement!r}")

    def _execute_insert(self, match: re.Match[str], params: list[Any]) -> Rows:
        table, cols_text, _placeholders, returning = match.groups()
        cols = [c.strip() for c in cols_text.split(",")]
        row = dict(zip(cols, params, strict=True))
        self._clock += 1
        if "id" not in row:
            self._next_id[table] = self._next_id.get(table, 0) + 1
            row["id"] = self._next_id[table]
        # Only `community_polls` has `created_at`/`updated_at` columns with a DB-side
        # `DEFAULT NOW()` (migration 028) -- `poll_options`/`poll_votes` do not, so this
        # fake must not fabricate columns the real schema doesn't have.
        if table == "community_polls":
            row.setdefault("created_at", self._clock)
            row.setdefault("updated_at", self._clock)
        self.tables.setdefault(table, []).append(row)
        return Rows(columns=[returning], rows=[[_wrap(row[returning])]], rows_affected=1)

    def _execute_update(self, match: re.Match[str], params: list[Any]) -> Rows:
        table, set_text, where = match.groups()
        assignments = [(col, int(idx)) for col, idx in _SET_RE.findall(set_text)]
        affected = 0
        for row in self.tables.get(table, []):
            if not self._row_matches(table, row, where, params):
                continue
            for col, idx in assignments:
                row[col] = params[idx - 1]
            affected += 1
        return Rows(columns=[], rows=[], rows_affected=affected)

    def _execute_select(self, match: re.Match[str], params: list[Any]) -> Rows:
        table, where = match.groups()
        rows = self.tables.get(table, [])
        if where:
            rows = [r for r in rows if self._row_matches(table, r, where, params)]
        columns = sorted({k for r in rows for k in r})
        wit_rows = [[_wrap(r.get(c)) for c in columns] for r in rows]
        return Rows(columns=columns, rows=wit_rows, rows_affected=len(rows))


def install(monkeypatch: Any) -> FakeWitDb:
    """Install a fresh `FakeWitDb` as `wit_world.imports.db` (merged into the existing fake
    `wit_world` module if one is already installed this test, e.g. by a `flags`/`relay`/`log`
    fixture) and return it."""
    import sys
    import types

    fake = FakeWitDb()
    db_mod = types.SimpleNamespace(
        execute=fake.execute,
        Value_NullValue=Value_NullValue,
        Value_BoolValue=Value_BoolValue,
        Value_IntValue=Value_IntValue,
        Value_FloatValue=Value_FloatValue,
        Value_TextValue=Value_TextValue,
        Value_BytesValue=Value_BytesValue,
        Rows=Rows,
    )
    existing = sys.modules.get("wit_world")
    if existing is not None and hasattr(existing, "imports"):
        existing.imports.db = db_mod
    else:
        fake_wit_world = types.ModuleType("wit_world")
        fake_wit_world.imports = types.SimpleNamespace(db=db_mod)  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return fake
