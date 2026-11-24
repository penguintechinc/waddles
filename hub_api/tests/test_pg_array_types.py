"""PG_TEXT_ARRAY / PG_INT_ARRAY round-trip and the app_catalog list regression."""

from __future__ import annotations

from typing import Any

import pytest
from pydal import DAL, Field

from services.pg_array_types import PG_INT_ARRAY, PG_TEXT_ARRAY, _encode, _split_literal


@pytest.fixture()
def db() -> Any:
    """In-memory SQLite DAL with one array-typed table."""
    dal = DAL("sqlite:memory")
    dal.define_table("t", Field("txt", PG_TEXT_ARRAY), Field("ints", PG_INT_ARRAY))
    return dal


def test_roundtrip_values_and_nulls(db: Any) -> None:
    db.t.insert(txt=["a", 'q"uote', "c,d", "back\\slash"], ints=[1, 2])
    db.t.insert(txt=[], ints=[])
    db.t.insert(txt=None, ints=None)
    rows = db(db.t).select(orderby=db.t.id)
    assert rows[0].txt == ["a", 'q"uote', "c,d", "back\\slash"]
    assert rows[0].ints == [1, 2]
    assert (rows[1].txt, rows[1].ints) == ([], [])
    assert (rows[2].txt, rows[2].ints) == (None, None)


def test_driver_list_is_accepted_as_is(db: Any) -> None:
    """regression: psycopg2 returns a list for TEXT[] -- pydal's list:string raised TypeError."""
    assert PG_TEXT_ARRAY.decoder(["x", "y"]) == ["x", "y"]
    assert PG_TEXT_ARRAY.decoder([]) == []
    assert PG_INT_ARRAY.decoder([3, 4]) == [3, 4]


def test_encode_rejects_bare_string_and_literal_parser_rejects_garbage() -> None:
    with pytest.raises(TypeError):
        _encode("abc")
    with pytest.raises(ValueError):
        _split_literal("|a|b|")


def test_app_catalog_lists_decode_after_bind() -> None:
    """regression: GET /marketplace/bundles 500 -- app_catalog array/jsonb columns unparsable."""
    from services.schema import bind_lifecycle_tables

    dal = DAL("sqlite:memory")
    bind_lifecycle_tables(dal, migrate=True)
    dal.app_catalog.insert(
        app_id="x.y.z",
        manifest_version="1.0.0",
        module="m",
        feature="f",
        provider="p",
        execution_model="wasm",
        compatible_with=["a"],
        incompatible_with=[],
        platform_compatibility={},
        alternative_to=["other.app"],
    )
    row = dal(dal.app_catalog.app_id == "x.y.z").select().first()
    assert row.compatible_with == ["a"]
    assert row.incompatible_with == []
    assert row.alternative_to == ["other.app"]
