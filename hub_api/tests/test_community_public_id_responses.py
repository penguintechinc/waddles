"""Response-side community public-id helpers: batched lookup, fail-loud, sync twin.

`lookup_public_ids` / `lookup_public_ids_sync` turn internal numeric community ids into the
public UUIDs every response DTO must carry, in ONE query, and must raise (never fall back to
the numeric id) when a community has no `public_id`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydal import DAL, Field
from quart import Quart

from services.community_public_id import lookup_public_ids, lookup_public_ids_sync
from tests.community_ids import install_community_ids


@pytest.fixture
def dal() -> Any:
    """In-memory sqlite DAL with a minimal `communities` table and three rows."""
    db = DAL("sqlite:memory", migrate=True)
    db.define_table("communities", Field("name", "string"), Field("public_id", "string", length=36))
    ids = [db.communities.insert(name=n) for n in ("a", "b", "c")]
    db.commit()
    db._seeded = ids  # type: ignore[attr-defined]
    return db


def _app(dal: Any) -> Quart:
    app = Quart(__name__)
    app.config["dal"] = dal
    install_community_ids(app, dal)
    return app


@pytest.mark.asyncio
async def test_batched_lookup_returns_uuid_strings_for_every_id(dal: Any) -> None:
    ids = dal._seeded
    expected = {}
    for cid in ids:
        expected[cid] = str(uuid.uuid4())
        dal(dal.communities.id == cid).update(public_id=expected[cid])
    dal.commit()
    app = _app(dal)
    async with app.app_context():
        got = await lookup_public_ids(ids + ids)  # duplicates collapse
    assert got == expected
    assert all(isinstance(v, str) and "-" in v for v in got.values())


@pytest.mark.asyncio
async def test_lookup_empty_input_is_empty_mapping(dal: Any) -> None:
    app = _app(dal)
    async with app.app_context():
        assert await lookup_public_ids([]) == {}


@pytest.mark.asyncio
async def test_missing_public_id_raises_instead_of_leaking_numeric_id(dal: Any) -> None:
    ids = dal._seeded
    dal(dal.communities.id == ids[0]).update(public_id=str(uuid.uuid4()))
    dal.commit()
    app = _app(dal)
    async with app.app_context():
        with pytest.raises(RuntimeError, match="missing public_id"):
            await lookup_public_ids(ids)


@pytest.mark.asyncio
async def test_unknown_community_id_raises(dal: Any) -> None:
    app = _app(dal)
    async with app.app_context():
        with pytest.raises(RuntimeError):
            await lookup_public_ids([999_999])


def test_sync_twin_maps_and_fails_loud(dal: Any) -> None:
    ids = dal._seeded
    pid = str(uuid.uuid4())
    dal(dal.communities.id == ids[0]).update(public_id=pid)
    dal.commit()
    assert lookup_public_ids_sync(dal, [ids[0]]) == {ids[0]: pid}
    with pytest.raises(RuntimeError, match="missing public_id"):
        lookup_public_ids_sync(dal, ids)
    assert lookup_public_ids_sync(dal, []) == {}
