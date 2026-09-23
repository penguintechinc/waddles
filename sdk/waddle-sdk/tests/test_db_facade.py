"""Tests for `waddle_sdk.db` -- the penguin_dal-compatible facade over the WIT `db` import.

Runs entirely host-side against `wit_fake_db.FakeWitDb`, a test double built
to the exact shape `componentize-py bindings` generates for the committed
`wit/waddle-bundle/stage.wit` (see `wit_shapes.py`). This is where the SQL
generation and WIT value marshalling logic gets its coverage.
"""

from __future__ import annotations

from typing import Any

import pytest
import wit_fake_db
import wit_shapes

from waddle_sdk.db import (
    DALError,
    DatabaseManager,
    Row,
    Rows,
    TableNotFoundError,
    ValidationError,
    create_dal,
    register_primary_key,
)


def _run(coro: Any) -> Any:
    """Drive a coroutine to completion without a real event loop.

    The facade's own coroutines never actually suspend (D21: "single-threaded
    ... with async-compatible signatures"), so a bare `send(None)` loop is
    sufficient and avoids depending on `_poll_loop.PollLoop` from this
    pure-facade test file.
    """
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("facade coroutine unexpectedly suspended")


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> wit_fake_db.FakeWitDb:
    """Install a fresh fake WIT `db` import for one test."""
    return wit_fake_db.install(monkeypatch)


def test_select_with_eq_query_generates_correct_sql(fake_db: wit_fake_db.FakeWitDb) -> None:
    """A combined `&` query lowers to `WHERE a = $1 AND b = $2` with positional params."""
    db = create_dal()
    query = (db.command_aliases.community_id == 42) & (db.command_aliases.alias == "bar")
    rows = _run(db(query).select())
    assert isinstance(rows, Rows)
    sql, params = fake_db.calls[0]
    assert "SELECT * FROM command_aliases WHERE" in sql
    assert "command_aliases.community_id = $1" in sql
    assert "command_aliases.alias = $2" in sql
    assert params == [42, "bar"]


def test_is_null_query(fake_db: wit_fake_db.FakeWitDb) -> None:
    """`FieldProxy.__eq__(None)` lowers to `IS NULL`, no placeholder consumed."""
    db = create_dal()
    query = db.command_aliases.deleted_at == None  # noqa: E711 - deliberately exercising the IS NULL path
    _run(db(query).select())
    sql, params = fake_db.calls[0]
    assert "command_aliases.deleted_at IS NULL" in sql
    assert params == []


def test_is_not_null_query(fake_db: wit_fake_db.FakeWitDb) -> None:
    """`FieldProxy.__ne__(None)` lowers to `IS NOT NULL`."""
    db = create_dal()
    query = db.command_aliases.deleted_at != None  # noqa: E711
    _run(db(query).select())
    sql, _ = fake_db.calls[0]
    assert "command_aliases.deleted_at IS NOT NULL" in sql


def test_comparison_operators(fake_db: wit_fake_db.FakeWitDb) -> None:
    """`>`, `<`, `>=`, `<=`, and plain `!=` all lower to the expected operator."""
    db = create_dal()
    for op, expected in (
        (db.t.a > 1, "t.a > $1"),
        (db.t.a < 1, "t.a < $1"),
        (db.t.a >= 1, "t.a >= $1"),
        (db.t.a <= 1, "t.a <= $1"),
        (db.t.a != 1, "t.a != $1"),
    ):
        _run(db(op).select())
        sql, params = fake_db.calls[-1]
        assert expected in sql
        assert params == [1]


def test_update_generates_set_and_where(fake_db: wit_fake_db.FakeWitDb) -> None:
    """UPDATE returns `rows_affected` from the WIT `Rows` record, not `len(rows)`."""
    fake_db.canned_rows_affected["UPDATE command_aliases"] = 1
    db = create_dal()
    query = db.command_aliases.id == 7
    rowcount = _run(db(query).update(deleted_at="2026-09-14T00:00:00+00:00"))
    sql, params = fake_db.calls[0]
    assert sql.startswith("UPDATE command_aliases SET deleted_at = $1 WHERE")
    assert params == ["2026-09-14T00:00:00+00:00", 7]
    assert rowcount == 1


def test_update_without_query(fake_db: wit_fake_db.FakeWitDb) -> None:
    """UPDATE with no WHERE clause (table-wide) updates every row."""
    from waddle_sdk.db import AsyncQuerySet

    fake_db.canned_rows_affected["UPDATE t SET"] = 3
    qs = AsyncQuerySet("t", None)
    rowcount = _run(qs.update(x=1))
    sql, params = fake_db.calls[0]
    assert sql == "UPDATE t SET x = $1"
    assert params == [1]
    assert rowcount == 3


def test_insert_generates_insert_into_with_returning_id(fake_db: wit_fake_db.FakeWitDb) -> None:
    """INSERT appends `RETURNING id` and the facade returns the new row's id."""
    fake_db.canned_rows["INSERT INTO command_aliases"] = [{"id": 55}]
    db = create_dal()
    new_id = _run(
        db.command_aliases.async_insert(
            community_id=42, alias="foo", target_command="ping", created_by="penguin"
        )
    )
    sql, params = fake_db.calls[0]
    assert sql == (
        "INSERT INTO command_aliases (community_id, alias, target_command, created_by) "
        "VALUES ($1, $2, $3, $4) RETURNING id"
    )
    assert params == [42, "foo", "ping", "penguin"]
    assert new_id == 55


def test_async_insert_returns_none_when_no_row_comes_back(fake_db: wit_fake_db.FakeWitDb) -> None:
    """If the stage returns no row, async_insert() returns None rather than raising."""
    db = create_dal()
    result = _run(db.command_aliases.async_insert(a=1))
    assert result is None


def test_async_bulk_insert_calls_async_insert_per_row(fake_db: wit_fake_db.FakeWitDb) -> None:
    """async_bulk_insert() issues one INSERT per row."""
    db = create_dal()
    _run(db.command_aliases.async_bulk_insert([{"a": 1}, {"a": 2}]))
    assert len(fake_db.calls) == 2


def test_delete_generates_delete_from(fake_db: wit_fake_db.FakeWitDb) -> None:
    """DELETE returns the WIT `rows_affected` count."""
    fake_db.canned_rows_affected["DELETE FROM command_aliases"] = 1
    db = create_dal()
    query = db.command_aliases.id == 7
    rowcount = _run(db(query).delete())
    sql, params = fake_db.calls[0]
    assert sql == "DELETE FROM command_aliases WHERE command_aliases.id = $1"
    assert params == [7]
    assert rowcount == 1


def test_delete_without_query(fake_db: wit_fake_db.FakeWitDb) -> None:
    """DELETE with no WHERE clause deletes the whole table."""
    from waddle_sdk.db import AsyncQuerySet

    qs = AsyncQuerySet("t", None)
    _run(qs.delete())
    sql, params = fake_db.calls[0]
    assert sql == "DELETE FROM t"
    assert params == []


def test_count_and_exists(fake_db: wit_fake_db.FakeWitDb) -> None:
    """COUNT(*) and the EXISTS-style probe both parse correctly."""
    db = create_dal()
    fake_db.canned_rows["SELECT COUNT(*)"] = [{"count": 3}]
    count = _run(db(db.command_aliases.community_id == 42).count())
    assert count == 3
    fake_db.canned_rows["SELECT 1 as exists FROM command_aliases"] = [{"exists": 1}]
    exists = _run(db(db.command_aliases.community_id == 42).exists())
    assert exists is True


def test_count_returns_zero_for_no_rows(fake_db: wit_fake_db.FakeWitDb) -> None:
    """COUNT(*) with no matching canned response returns 0, never raises."""
    db = create_dal()
    count = _run(db(db.command_aliases.community_id == 999).count())
    assert count == 0


def test_select_without_query_selects_whole_table(fake_db: wit_fake_db.FakeWitDb) -> None:
    """`db(None)`-less select (no WHERE) selects every row."""
    from waddle_sdk.db import AsyncQuerySet

    qs = AsyncQuerySet("t", None)
    _run(qs.select())
    sql, params = fake_db.calls[0]
    assert sql == "SELECT * FROM t"
    assert params == []


def test_select_with_explicit_columns(fake_db: wit_fake_db.FakeWitDb) -> None:
    """select(*columns) projects only the named columns."""
    db = create_dal()
    _run(db(db.t.a == 1).select(db.t.a, db.t.b))
    sql, _ = fake_db.calls[0]
    assert sql.startswith("SELECT t.a, t.b FROM t WHERE")


def test_select_orderby_or_limitby_raises_not_implemented(fake_db: wit_fake_db.FakeWitDb) -> None:
    """orderby/limitby are accepted for signature compatibility but not lowered."""
    db = create_dal()
    with pytest.raises(NotImplementedError, match="orderby"):
        _run(db(db.t.a == 1).select(orderby="a"))
    with pytest.raises(NotImplementedError, match="orderby"):
        _run(db(db.t.a == 1).select(limitby=(0, 10)))


def test_getitem_pk_lookup_returns_row_or_none(fake_db: wit_fake_db.FakeWitDb) -> None:
    """`db.table[pk]` is synchronous and returns a Row or None."""
    db = create_dal()
    fake_db.canned_rows["command_aliases.id = $1"] = [{"id": 7, "alias": "bar"}]
    row = db.command_aliases[7]
    assert isinstance(row, Row)
    assert row.alias == "bar"
    assert row["id"] == 7

    fake_db.canned_rows.clear()
    missing = db.command_aliases[999]
    assert missing is None


def test_getitem_and_insert_use_registered_non_id_primary_key(
    fake_db: wit_fake_db.FakeWitDb,
) -> None:
    """A table registered with a non-`id` PK uses that column, never a hardcoded `id`.

    Regression: `__getitem__`/`async_insert` used to hardcode `WHERE
    {table}.id = $1` / `RETURNING id` unconditionally.
    """
    register_primary_key("widgets", "widget_uuid")
    db = create_dal()

    fake_db.canned_rows["widgets.widget_uuid = $1"] = [{"widget_uuid": "w-1", "name": "gizmo"}]
    row = db.widgets["w-1"]
    assert row is not None
    assert row.name == "gizmo"
    select_sql, select_params = fake_db.calls[0]
    assert "widgets.widget_uuid = $1" in select_sql
    assert "widgets.id" not in select_sql
    assert select_params == ["w-1"]

    fake_db.canned_rows["INSERT INTO widgets"] = [{"widget_uuid": "w-2"}]
    new_pk = _run(db.widgets.async_insert(name="sprocket"))
    insert_sql, _ = fake_db.calls[1]
    assert insert_sql.endswith("RETURNING widget_uuid")
    assert new_pk == "w-2"


def test_unregistered_table_still_defaults_to_id_primary_key(
    fake_db: wit_fake_db.FakeWitDb,
) -> None:
    """Without a registration, `id` remains the default -- matches every first-party bundle."""
    db = create_dal()
    fake_db.canned_rows["command_aliases.id = $1"] = [{"id": 3, "alias": "foo"}]
    row = db.command_aliases[3]
    assert row is not None
    sql, _ = fake_db.calls[0]
    assert "command_aliases.id = $1" in sql


def test_registering_none_primary_key_raises_not_implemented_never_silently_emits_id(
    fake_db: wit_fake_db.FakeWitDb,
) -> None:
    """Registering `pk_column=None` (e.g. a composite key) raises, never falls back to `id`."""
    register_primary_key("composite_keyed", None)
    db = create_dal()
    with pytest.raises(NotImplementedError, match="non-'id' primary key not supported"):
        db.composite_keyed[1]
    with pytest.raises(NotImplementedError, match="composite_keyed"):
        _run(db.composite_keyed.async_insert(a=1))
    assert len(fake_db.calls) == 0  # never crossed the WIT boundary with a guessed column


def test_rows_supports_dict_and_attribute_access_and_iteration(
    fake_db: wit_fake_db.FakeWitDb,
) -> None:
    """Rows/Row support len, iteration, first(), and dict-shaped export."""
    fake_db.canned_rows["command_aliases"] = [{"id": 1, "alias": "a"}, {"id": 2, "alias": "b"}]
    db = create_dal()
    rows = _run(db(db.command_aliases.id > 0).select())
    assert len(rows) == 2
    assert rows.first().alias == "a"
    assert rows.last().alias == "b"
    assert rows[0].alias == "a"
    assert bool(rows) is True
    assert [r["alias"] for r in rows] == ["a", "b"]
    assert rows.as_list() == [{"id": 1, "alias": "a"}, {"id": 2, "alias": "b"}]
    row = rows.first()
    assert "alias" in row
    assert row.keys() == ["id", "alias"]
    assert row.values() == [1, "a"]
    assert row.items() == [("id", 1), ("alias", "a")]
    assert row.get("missing", "default") == "default"
    assert row == Row({"id": 1, "alias": "a"})
    assert row != "not a row"


def test_rows_empty_is_falsy(fake_db: wit_fake_db.FakeWitDb) -> None:
    """An empty Rows is falsy and first()/last() return None."""
    rows = Rows([])
    assert bool(rows) is False
    assert rows.first() is None
    assert rows.last() is None


def test_row_missing_attribute_raises_attribute_error() -> None:
    """Row.__getattr__ raises AttributeError for a missing column."""
    row = Row({"id": 1})
    with pytest.raises(AttributeError):
        _ = row.nonexistent
    with pytest.raises(AttributeError):
        _ = row._private


def test_query_and_or_combine_correctly(fake_db: wit_fake_db.FakeWitDb) -> None:
    """`&` and `|` combine query fragments with the correct SQL operator and param order."""
    db = create_dal()
    query = (db.command_aliases.a == 1) | (db.command_aliases.b == 2)
    _run(db(query).select())
    sql, params = fake_db.calls[0]
    assert "(command_aliases.a = $1) OR (command_aliases.b = $2)" in sql
    assert params == [1, 2]


def test_unsupported_field_constructs_raise_not_implemented_error_never_misexecute(
    fake_db: wit_fake_db.FakeWitDb,
) -> None:
    """Every unimplemented FieldProxy construct raises, never silently mis-executes."""
    db = create_dal()
    with pytest.raises(NotImplementedError):
        db.command_aliases.alias.like("%foo%")
    with pytest.raises(NotImplementedError):
        db.command_aliases.alias.ilike("%foo%")
    with pytest.raises(NotImplementedError):
        db.command_aliases.alias.contains("foo")
    with pytest.raises(NotImplementedError):
        db.command_aliases.alias.startswith("foo")
    with pytest.raises(NotImplementedError):
        db.command_aliases.alias.endswith("foo")
    with pytest.raises(NotImplementedError):
        db.command_aliases.alias.belongs(["a", "b"])
    assert len(fake_db.calls) == 0  # not one of these ever crossed the WIT boundary


def test_sync_insert_and_bulk_insert_raise_not_implemented(fake_db: wit_fake_db.FakeWitDb) -> None:
    """The sync `insert()`/`bulk_insert()` variants are explicit gaps -- use the async_ ones."""
    db = create_dal()
    with pytest.raises(NotImplementedError, match="async_insert"):
        db.command_aliases.insert(a=1)
    with pytest.raises(NotImplementedError, match="async_bulk_insert"):
        db.command_aliases.bulk_insert([{"a": 1}])


def test_execute_raw_sql_passthrough(fake_db: wit_fake_db.FakeWitDb) -> None:
    """AsyncDB.execute() is a thin passthrough to _cross()."""
    db = create_dal()
    rows = _run(db.execute("SELECT role FROM community_members WHERE community_id = $1", [42]))
    sql, params = fake_db.calls[0]
    assert sql == "SELECT role FROM community_members WHERE community_id = $1"
    assert params == [42]
    assert rows == []


def test_execute_with_no_params(fake_db: wit_fake_db.FakeWitDb) -> None:
    """AsyncDB.execute() defaults params to an empty list."""
    db = create_dal()
    _run(db.execute("SELECT 1"))
    _, params = fake_db.calls[0]
    assert params == []


def test_call_without_query_raises_validation_error() -> None:
    """AsyncDB.__call__(None) has no table to scope to and raises ValidationError."""
    db = create_dal()
    with pytest.raises(ValidationError):
        db(None)


def test_engine_property_raises_not_implemented() -> None:
    """AsyncDB.engine is an explicit, named gap -- there is no live engine in the sandbox."""
    db = create_dal()
    with pytest.raises(NotImplementedError, match="engine"):
        _ = db.engine


def test_commit_and_close_are_noops(fake_db: wit_fake_db.FakeWitDb) -> None:
    """commit()/close() are no-ops -- every statement already committed host-side."""
    db = create_dal()
    assert _run(db.commit()) is None
    assert _run(db.close()) is None


def test_database_manager_raises_not_implemented() -> None:
    """DatabaseManager cannot connect to anything inside the sandbox."""
    with pytest.raises(NotImplementedError, match="DatabaseManager"):
        DatabaseManager("postgresql://primary/db")


def test_db_execute_wraps_wit_error_as_dalerror(fake_db: wit_fake_db.FakeWitDb) -> None:
    """A WIT `db.Error` on the result's Err arm is re-raised as DALError, never swallowed."""
    fake_db.raise_on["command_aliases"] = wit_shapes.DbError_Backend(value="connection reset")
    db = create_dal()
    with pytest.raises(DALError, match="connection reset"):
        _run(db(db.command_aliases.id == 1).select())


def test_field_proxy_hash_and_repr() -> None:
    """FieldProxy is hashable (by table/column identity) and has a debug repr."""
    db = create_dal()
    f1 = db.t.a
    f2 = db.t.a
    assert hash(f1) == hash(f2)
    assert "FieldProxy" in repr(f1)


def test_table_proxy_repr_and_table_name() -> None:
    """TableProxy exposes `.table_name` and a debug repr."""
    db = create_dal()
    proxy = db.command_aliases
    assert proxy.table_name == "command_aliases"
    assert "TableProxy" in repr(proxy)


def test_underscore_attribute_access_raises_attribute_error() -> None:
    """AsyncDB/TableProxy never treat a dunder/private name as a table/column."""
    db = create_dal()
    with pytest.raises(AttributeError):
        _ = db._private
    with pytest.raises(AttributeError):
        _ = db.t._private


def test_table_not_found_error_and_validation_error_are_dal_errors() -> None:
    """TableNotFoundError/ValidationError both subclass DALError."""
    assert issubclass(TableNotFoundError, DALError)
    assert issubclass(ValidationError, DALError)


def test_from_wit_value_rejects_unrecognized_case() -> None:
    """An unrecognized WIT db.Value case raises NotImplementedError naming it, never mis-decodes."""
    from waddle_sdk.db import _from_wit_value

    class _NotARealValueCase:
        pass

    with pytest.raises(NotImplementedError, match="unrecognized"):
        _from_wit_value(_NotARealValueCase())


def test_coerce_param_handles_uuid_dict_list_datetime_date() -> None:
    """`_coerce_param` matches real DAL param conversion for non-primitive types."""
    import json
    import uuid
    from datetime import date, datetime

    from waddle_sdk.db import _coerce_param

    u = uuid.uuid4()
    assert _coerce_param(u) == str(u)
    assert _coerce_param({"a": 1}) == json.dumps({"a": 1})
    assert _coerce_param([1, 2]) == json.dumps([1, 2])
    d = datetime(2026, 9, 14, 12, 0, 0)
    assert _coerce_param(d) == d.isoformat()
    dt = date(2026, 9, 14)
    assert _coerce_param(dt) == dt.isoformat()
    assert _coerce_param("plain") == "plain"


def test_bool_is_wrapped_as_bool_value_not_int_value(fake_db: wit_fake_db.FakeWitDb) -> None:
    """Bool is an int subclass in Python -- must not be misrouted to Value_IntValue."""
    import sys

    from waddle_sdk.db import _to_wit_value

    db_mod = sys.modules["wit_world"].imports.db
    wrapped = _to_wit_value(db_mod, True)
    assert isinstance(wrapped, wit_shapes.Value_BoolValue)
    assert wrapped.value is True


def test_bytes_and_null_round_trip_through_select(fake_db: wit_fake_db.FakeWitDb) -> None:
    """Bytes and None values round-trip correctly through the WIT Value union."""
    fake_db.canned_rows["blob_table"] = [{"data": b"\x00\x01", "note": None}]
    db = create_dal()
    rows = _run(db(db.blob_table.id == 1).select())
    row = rows.first()
    assert row["data"] == b"\x00\x01"
    assert row["note"] is None
