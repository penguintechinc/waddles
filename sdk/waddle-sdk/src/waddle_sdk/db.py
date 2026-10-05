"""Structured table-ops facade over the WIT ``db`` import (stage.wit).

**Structured, never raw SQL** (user decision: bundles use structured ``db``
ops, not a raw-SQL arm; design doc SS1 round-1 CRITICAL finding: "no
bundle-supplied SQL, ever"). Every statement used to cross this boundary as
``db.execute(statement, params)``; that shape is retired. The WIT ``db``
interface now exposes exactly five ops -- ``insert``/``get``/``query``/
``update``/``delete`` -- matching ``core/bundle_host_db::DbHost`` byte for
byte, enforced host-side under the least-privilege ``waddles_bundle_runtime``
Postgres role.

**No ``table`` parameter, by design.** Unlike a general-purpose DAL, a
bundle owns exactly ONE table (its own, in ``app_core``/``app_community``,
provisioned at install time from the manifest's ``data.table.columns``) --
every op below implicitly targets that single table; there is nothing to
name. Tenant/community/app scoping is applied server-side from the
invocation's own authenticated scope, never from guest-supplied input
(``bundle_host_db::scope::DbScope``'s own doc).

**Binding shapes below are not guessed.** Confirmed by running
``componentize-py==0.25.1``'s ``bindings`` subcommand against the committed
``wit/waddle-bundle/stage.wit``: the WIT ``variant value`` becomes one
``@dataclass`` per case (``Value_NullValue``, ``Value_BoolValue(value: bool)``,
``Value_IntValue(value: int)``, ``Value_FloatValue(value: float)``,
``Value_TextValue(value: str)``, ``Value_BytesValue(value: bytes)``), the
``record column-value`` becomes ``ColumnValue(column: str, value: Value)``,
and ``record row`` becomes ``Row(row_id: str, version: int,
columns: list[ColumnValue])`` -- all module attributes of the generated
``wit_world.imports.db`` module. Each op raises the generated ``Err`` (a
frozen dataclass ``Exception`` subclass with one attribute, ``value``,
holding the ``db.error`` variant) on failure; this module classifies it
structurally via ``getattr(exc, "value", exc)``, the same pattern
``waddle_sdk.kv``/``waddle_sdk.http`` already use for their own WIT error
variants.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any
from uuid import UUID


class DbError(Exception):
    """Base exception for this facade -- wraps a WIT ``db.error`` variant."""


class NotFoundError(DbError):
    """``row-id`` has no matching row for this app's own scope."""


class ConflictError(DbError):
    """``expected-version`` did not match the row's current version."""


class ValidationError(DbError):
    """``invalid-column``/``invalid-value`` -- a column/value was rejected."""


class QuotaExceededError(DbError):
    """Per-app row/byte quota exceeded."""


# Back-compat aliases -- the pre-structured facade raised `DALError`/
# `ValidationError` (its `penguin_dal`-shaped names); `DALError` stays as an
# alias of this module's own base so a caller importing either name keeps
# working across the structured rewrite.
DALError = DbError


def _coerce_value(value: Any) -> Any:
    """Mirror real-DAL param conversion before a value is wrapped as a WIT ``value``.

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
    """Wrap one coerced Python scalar into the generated WIT ``db.value`` union."""
    coerced = _coerce_value(value)
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
    """Unwrap one generated WIT ``db.value`` union member back to a Python scalar."""
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


def _to_wit_column_values(db_mod: Any, row: dict[str, Any]) -> list[Any]:
    """Convert a plain ``{column: value}`` dict into the WIT ``list<column-value>`` shape."""
    return [
        db_mod.ColumnValue(column=column, value=_to_wit_value(db_mod, value))
        for column, value in row.items()
    ]


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a generated WIT ``db.row`` record into a plain dict.

    ``row_id``/``version`` (the platform-owned identity/optimistic-
    concurrency columns) are included alongside every declared column --
    callers needing just the declared columns can pop them, but losing them
    silently would make ``update``/``delete``'s ``expected_version``
    argument impossible to chain from a prior ``get``/``insert`` result.
    """
    data: dict[str, Any] = {cv.column: _from_wit_value(cv.value) for cv in row.columns}
    data["row_id"] = row.row_id
    data["version"] = row.version
    return data


def _raise_for(exc: Exception, op: str) -> None:
    """Classify a raised WIT ``db.error`` and re-raise as this module's own exception type.

    ``getattr(exc, "value", exc)`` reaches the real ``db.error`` variant
    without importing ``componentize_py_types`` (home of the generated
    ``Err`` wrapper) -- see this module's docstring for why: it is
    per-build-generated and not pip-installable.
    """
    detail = getattr(exc, "value", exc)
    type_name = type(detail).__name__
    message = f"db.{op} failed: {detail}"
    if type_name == "Error_NotFound":
        raise NotFoundError(message) from exc
    if type_name == "Error_Conflict":
        raise ConflictError(message) from exc
    if type_name in ("Error_InvalidColumn", "Error_InvalidValue"):
        raise ValidationError(message) from exc
    if type_name == "Error_QuotaExceeded":
        raise QuotaExceededError(message) from exc
    raise DbError(message) from exc


async def insert(row: dict[str, Any]) -> dict[str, Any]:
    """Insert one row; returns the stored row (with ``row_id``/``version``)."""
    import wit_world  # generated binding -- only resolvable inside a component

    db_mod = wit_world.imports.db
    try:
        result = db_mod.insert(_to_wit_column_values(db_mod, row))
    except Exception as exc:  # noqa: BLE001 - classified structurally, see module docstring
        _raise_for(exc, "insert")
        raise  # unreachable -- _raise_for always raises
    return _row_to_dict(result)


async def get(row_id: str) -> dict[str, Any] | None:
    """Fetch one row by ``row_id``; returns ``None`` if not found (never raises for that case)."""
    import wit_world

    db_mod = wit_world.imports.db
    try:
        result = db_mod.get(row_id)
    except Exception as exc:  # noqa: BLE001 - classified structurally, see module docstring
        detail = getattr(exc, "value", exc)
        if type(detail).__name__ == "Error_NotFound":
            return None
        _raise_for(exc, "get")
        raise  # unreachable
    return _row_to_dict(result)


async def query(
    limit: int = 200,
    offset: int = 0,
    order_by: str | None = None,
    descending: bool = False,
    random: bool = False,
) -> list[dict[str, Any]]:
    """Bounded, orderable list of this bundle's own rows.

    ``limit`` is clamped host-side (``bundle_host_db::MAX_QUERY_LIMIT``)
    regardless of what is requested here. Ordering, in priority order:

    - ``random=True``: ``ORDER BY random()`` -- pick uniformly, typically
      paired with ``limit=1`` (e.g. a quote/8-ball-style bundle's "one
      random row"). Mutually exclusive with ``order_by``.
    - ``order_by=<column>``: sort by that declared column (or a fixed
      sortable platform column -- ``row_id``/``version``/``created_at``/
      ``updated_at``), ``descending`` controlling direction. Validated
      host-side -- never a guest-supplied arbitrary SQL fragment.
    - Neither set: the host's own default, stable ``row_id`` ascending.

    Raises:
        ValueError: both ``random`` and ``order_by`` were set -- ambiguous,
            rejected here rather than silently picking one.
    """
    if random and order_by is not None:
        raise ValueError("query(): random=True and order_by=... are mutually exclusive")

    import wit_world

    db_mod = wit_world.imports.db
    wit_order_by = None
    if random:
        wit_order_by = db_mod.OrderBy_Random()
    elif order_by is not None:
        wit_order_by = db_mod.OrderBy_Column(
            db_mod.OrderColumn(name=order_by, descending=descending)
        )
    try:
        rows = db_mod.query(limit, offset, wit_order_by)
    except Exception as exc:  # noqa: BLE001 - classified structurally, see module docstring
        _raise_for(exc, "query")
        raise  # unreachable
    return [_row_to_dict(r) for r in rows]


async def update(row_id: str, expected_version: int, row: dict[str, Any]) -> dict[str, Any]:
    """Update one row, gated on ``expected_version`` (optimistic concurrency).

    Raises :class:`ConflictError` on a version mismatch, :class:`NotFoundError`
    if ``row_id`` no longer exists.
    """
    import wit_world

    db_mod = wit_world.imports.db
    try:
        result = db_mod.update(row_id, expected_version, _to_wit_column_values(db_mod, row))
    except Exception as exc:  # noqa: BLE001 - classified structurally, see module docstring
        _raise_for(exc, "update")
        raise  # unreachable
    return _row_to_dict(result)


async def delete(row_id: str, expected_version: int) -> None:
    """Delete one row, gated on ``expected_version`` the same way as :func:`update`."""
    import wit_world

    db_mod = wit_world.imports.db
    try:
        db_mod.delete(row_id, expected_version)
    except Exception as exc:  # noqa: BLE001 - classified structurally, see module docstring
        _raise_for(exc, "delete")
        raise  # unreachable


class Rows:
    """Inert legacy container -- retained only for a now-unused type reference.

    `waddle_sdk.flask_core.bundle_runtime`'s `raw_sql_rows`/`raw_sql_write`
    (already, independently, permanent `NotImplementedError` stubs -- see
    that module) keep a resolvable return-type name to import. The raw-SQL
    arm this type used to carry results for is retired (user decision:
    structured `db` ops, not raw SQL) -- nothing in this module ever
    constructs one anymore.
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        """Wrap an already-materialized list of row dicts, if any."""
        self.rows = rows or []


class AsyncDB:
    """Legacy compatibility placeholder for the component-bootstrap wiring.

    Retained only so `_component_entry.py`'s
    ``set_bundle_dal(AsyncDB())`` and the `flask_core`-compatible
    `get_bundle_dal()` shim keep importing and constructing successfully.

    **The DAL-style, multi-table query-builder facade this class used to
    implement is retired** (user decision: bundles use this module's
    structured `insert`/`get`/`query`/`update`/`delete` functions directly,
    never a `db.<table>.<column> == value` query builder lowering to raw
    SQL -- design doc SS1 round-1 CRITICAL finding: "no bundle-supplied SQL,
    ever"). Every attribute access past construction raises
    `NotImplementedError` naming the structured replacement, rather than
    silently mis-executing against a WIT `db` import that no longer has an
    `execute` op at all.
    """

    def __getattr__(self, name: str) -> Any:
        """Raise, naming the structured functions bundle authors should call instead."""
        if name.startswith("_"):
            raise AttributeError(name)
        raise NotImplementedError(
            f"AsyncDB.{name} is retired -- call waddle_sdk.db's structured "
            "insert()/get()/query()/update()/delete() functions directly instead "
            "of a DAL-style table proxy (user decision: structured db ops, not raw SQL)"
        )

    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        """Raise -- the old `db(query)` entry point is retired (see class docstring)."""
        raise NotImplementedError(
            "AsyncDB.__call__ (the DAL query-builder entry point) is retired -- call "
            "waddle_sdk.db's structured insert()/get()/query()/update()/delete() "
            "functions directly instead"
        )
