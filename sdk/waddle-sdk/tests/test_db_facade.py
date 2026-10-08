"""Tests for `waddle_sdk.db` -- the structured `db` capability client.

Runs entirely host-side against `wit_fake_db.FakeWitDb`, a test double built
to the exact shape `componentize-py bindings` generates for the committed
`wit/waddle-bundle/stage.wit` (see `wit_shapes.py`). Structured ops only
(insert/get/query/update/delete) -- the retired raw-SQL `execute` facade's
own test coverage lived here before this rewrite; see git history for that
version if ever needed for reference.

No pytest-asyncio dependency (same convention as `test_kv.py`): every
coroutine under test never actually suspends (synchronous WIT host calls
underneath), so `_run()`'s bare `send(None)` drive is sufficient.
"""

from __future__ import annotations

from typing import Any

import pytest
import wit_fake_db
import wit_shapes

from waddle_sdk.db import (
    AsyncDB,
    ConflictError,
    NotFoundError,
    QuotaExceededError,
    Rows,
    ValidationError,
    delete,
    get,
    insert,
    query,
    update,
)


def _run(coro: Any) -> Any:
    """Drive a coroutine to completion without a real event loop (see module docstring)."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("db facade coroutine unexpectedly suspended")


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> wit_fake_db.FakeWitDb:
    """Install a fresh fake WIT `db` import for one test."""
    return wit_fake_db.install(monkeypatch)


def test_insert_returns_row_id_version_and_columns(fake_db: wit_fake_db.FakeWitDb) -> None:
    """insert() round-trips a plain dict through the WIT column-value shape."""
    row = _run(insert({"score": 42, "name": "alice", "active": True, "note": None}))
    assert row["row_id"] == "00000000-0000-0000-0000-000000000001"
    assert row["version"] == 1
    assert row["score"] == 42
    assert row["name"] == "alice"
    assert row["active"] is True
    assert row["note"] is None


def test_insert_coerces_uuid_dict_and_datetime_values(fake_db: wit_fake_db.FakeWitDb) -> None:
    """UUID -> str, dict/list -> JSON text, datetime/date -> ISO text."""
    from datetime import date
    from uuid import UUID

    row = _run(
        insert(
            {
                "user_ref": UUID("12345678-1234-5678-1234-567812345678"),
                "payload": {"a": 1},
                "created": date(2026, 1, 1),
            }
        )
    )
    assert row["user_ref"] == "12345678-1234-5678-1234-567812345678"
    assert row["payload"] == '{"a": 1}'
    assert row["created"] == "2026-01-01"


def test_insert_handles_bytes(fake_db: wit_fake_db.FakeWitDb) -> None:
    """A `bytes` value round-trips through `Value_BytesValue`."""
    row = _run(insert({"blob": b"\x01\x02\x03"}))
    assert row["blob"] == b"\x01\x02\x03"


def test_insert_handles_float(fake_db: wit_fake_db.FakeWitDb) -> None:
    """A `float` value round-trips through `Value_FloatValue`."""
    row = _run(insert({"ratio": 0.5}))
    assert row["ratio"] == 0.5


def test_insert_maps_an_unrecognized_error_case_to_the_base_exception(
    fake_db: wit_fake_db.FakeWitDb,
) -> None:
    """A `db.error` case this module has no specific exception for falls back to `DbError`."""
    fake_db.raise_on["insert"] = wit_shapes.Err(wit_shapes.DbError_Backend("connection lost"))
    with pytest.raises(Exception, match="db.insert failed") as exc_info:
        _run(insert({"score": 1}))
    from waddle_sdk.db import DbError

    assert isinstance(exc_info.value, DbError)


def test_get_returns_none_when_not_found(fake_db: wit_fake_db.FakeWitDb) -> None:
    """get() on a never-inserted row_id returns None, never raises."""
    assert _run(get("00000000-0000-0000-0000-000000000099")) is None


def test_get_returns_the_inserted_row(fake_db: wit_fake_db.FakeWitDb) -> None:
    """get() after insert() returns the same row data."""

    async def body() -> dict[str, Any] | None:
        inserted = await insert({"score": 7})
        return await get(inserted["row_id"])

    fetched = _run(body())
    assert fetched is not None
    assert fetched["score"] == 7
    assert fetched["version"] == 1


def test_query_returns_bounded_list_ordered_by_row_id(fake_db: wit_fake_db.FakeWitDb) -> None:
    """query() lists every inserted row, ordered by row_id, respecting limit/offset."""

    async def body() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        for i in range(3):
            await insert({"n": i})
        all_rows = await query(limit=200, offset=0)
        page = await query(limit=1, offset=1)
        return all_rows, page

    all_rows, page = _run(body())
    assert len(all_rows) == 3
    assert [r["n"] for r in all_rows] == [0, 1, 2]
    assert len(page) == 1
    assert page[0]["n"] == 1


def test_query_sorts_by_a_declared_column(fake_db: wit_fake_db.FakeWitDb) -> None:
    """query(order_by=...) sorts ascending by the named column."""

    async def body() -> list[dict[str, Any]]:
        await insert({"score": 3})
        await insert({"score": 1})
        await insert({"score": 2})
        return await query(order_by="score")

    rows = _run(body())
    assert [r["score"] for r in rows] == [1, 2, 3]


def test_query_sorts_descending(fake_db: wit_fake_db.FakeWitDb) -> None:
    """query(order_by=..., descending=True) reverses the sort direction."""

    async def body() -> list[dict[str, Any]]:
        await insert({"score": 1})
        await insert({"score": 3})
        await insert({"score": 2})
        return await query(order_by="score", descending=True)

    rows = _run(body())
    assert [r["score"] for r in rows] == [3, 2, 1]


def test_query_random_and_order_by_are_mutually_exclusive() -> None:
    """query(random=True, order_by=...) is rejected before ever crossing the WIT boundary."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        _run(query(random=True, order_by="score"))


def test_query_random_reaches_the_host_call(fake_db: wit_fake_db.FakeWitDb) -> None:
    """query(random=True) sends an `OrderBy_Random` -- proves the variant round-trips."""

    async def body() -> list[dict[str, Any]]:
        await insert({"score": 1})
        await insert({"score": 2})
        return await query(limit=1, random=True)

    rows = _run(body())
    assert len(rows) == 1
    _, _, order_by = fake_db.calls[-1][1]
    assert type(order_by).__name__ == "OrderBy_Random"


def test_update_requires_matching_expected_version(fake_db: wit_fake_db.FakeWitDb) -> None:
    """update() with a stale expected_version raises ConflictError."""

    async def body() -> dict[str, Any]:
        inserted = await insert({"score": 1})
        updated = await update(inserted["row_id"], inserted["version"], {"score": 2})
        with pytest.raises(ConflictError):
            await update(inserted["row_id"], 1, {"score": 3})
        return updated

    updated = _run(body())
    assert updated["score"] == 2
    assert updated["version"] == 2


def test_update_on_missing_row_raises_not_found(fake_db: wit_fake_db.FakeWitDb) -> None:
    """update() on a row_id that was never inserted raises NotFoundError."""
    with pytest.raises(NotFoundError):
        _run(update("00000000-0000-0000-0000-000000000099", 1, {"score": 1}))


def test_delete_removes_the_row(fake_db: wit_fake_db.FakeWitDb) -> None:
    """delete() removes the row; a subsequent get() returns None."""

    async def body() -> dict[str, Any] | None:
        inserted = await insert({"score": 1})
        await delete(inserted["row_id"], inserted["version"])
        return await get(inserted["row_id"])

    assert _run(body()) is None


def test_delete_on_missing_row_raises_not_found(fake_db: wit_fake_db.FakeWitDb) -> None:
    """delete() on a never-inserted row_id raises NotFoundError."""
    with pytest.raises(NotFoundError):
        _run(delete("00000000-0000-0000-0000-000000000099", 1))


def test_delete_with_stale_version_raises_conflict(fake_db: wit_fake_db.FakeWitDb) -> None:
    """delete() with a stale expected_version raises ConflictError, row survives."""

    async def body() -> dict[str, Any] | None:
        inserted = await insert({"score": 1})
        with pytest.raises(ConflictError):
            await delete(inserted["row_id"], inserted["version"] + 1)
        return await get(inserted["row_id"])

    assert _run(body()) is not None


@pytest.mark.parametrize(
    ("error_case", "expected_exception"),
    [
        (wit_shapes.DbError_InvalidColumn("bad column"), ValidationError),
        (wit_shapes.DbError_InvalidValue("bad value"), ValidationError),
        (wit_shapes.DbError_QuotaExceeded("too many rows"), QuotaExceededError),
    ],
)
def test_insert_maps_every_wit_error_case(
    fake_db: wit_fake_db.FakeWitDb,
    error_case: Any,
    expected_exception: type[Exception],
) -> None:
    """Every `db.error` variant insert() can raise maps to this module's own exception type."""
    fake_db.raise_on["insert"] = wit_shapes.Err(error_case)
    with pytest.raises(expected_exception):
        _run(insert({"score": 1}))


def test_rows_is_a_plain_inert_container() -> None:
    """Rows is retained only as a resolvable name; it is never constructed by this module."""
    rows = Rows([{"a": 1}])
    assert rows.rows == [{"a": 1}]
    assert Rows().rows == []


def test_async_db_is_constructible_but_every_op_raises_not_implemented() -> None:
    """AsyncDB exists for `_component_entry.py`'s bootstrap but is otherwise inert."""
    dal = AsyncDB()
    with pytest.raises(NotImplementedError, match="retired"):
        _ = dal.some_table
    with pytest.raises(NotImplementedError, match="retired"):
        dal(None)


def test_async_db_dunder_attribute_access_raises_attribute_error() -> None:
    """A private attribute probe gets `AttributeError`, not `NotImplementedError`."""
    dal = AsyncDB()
    with pytest.raises(AttributeError):
        _ = dal._private_attr
