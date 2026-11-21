"""Generic entity public-id helper: `<puuid:...>` converter, scoped resolver, translator hook.

Covers `services/public_ids.py`: strict UUID routing (a numeric id 404s), `Scope` fail-loud
construction, in-scope resolution, the identical `None` for malformed / unknown / out-of-scope
UUIDs (no existence oracle), batched response lookup that never falls back to the numeric id, and
the `before_request` translator that keeps handlers on `int` ids.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from pydal import DAL, Field
from quart import Quart

from services.public_ids import (
    UNKNOWN_ID,
    PublicId,
    PublicIdTarget,
    Scope,
    build_view_arg_translator,
    lookup_public_ids,
    parse_public_id,
    register_default_converter,
    resolve_public_id,
    resolve_public_id_or_unknown,
    resolve_public_id_sync,
)

PUB_A = "0b6b0a52-5f0e-4f6a-9a39-1b6a3d6d9f01"
PUB_B = "7c1d2e3f-4a5b-4c6d-8e7f-0a1b2c3d4e5f"
PUB_C = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def dal(tmp_path: Path) -> Any:
    """File-backed sqlite DAL (worker threads see it) with a tenant+community scoped table."""
    db = DAL(f"sqlite://{tmp_path}/pid.db", folder=str(tmp_path), migrate=True)
    db.define_table(
        "things",
        Field("name", "string"),
        Field("tenant_id", "integer"),
        Field("community_id", "integer"),
        Field("public_id", "string", length=36),
    )
    db.define_table("globals", Field("name", "string"), Field("public_id", "string", length=36))
    db.define_table("legacy", Field("name", "string"))
    db.things.insert(name="a", tenant_id=1, community_id=10, public_id=PUB_A)
    db.things.insert(name="b", tenant_id=2, community_id=20, public_id=PUB_B)
    db.globals.insert(name="g", public_id=PUB_C)
    db.commit()
    return db


class FakeAsyncDal:
    """`select_async(query, *fields)` over a pydal DAL, as `AsyncDAL` exposes it."""

    async def select_async(self, set_: Any, *fields: Any) -> list[Any]:
        """Run the pydal set synchronously and return its rows."""
        return list(set_.select(*fields))


def test_parse_public_id_is_strict() -> None:
    assert parse_public_id(PUB_A.upper()) == PUB_A
    assert parse_public_id(f"  {PUB_A} ") == PUB_A
    for bad in ("12", PUB_A.replace("-", ""), None, 7, True, "", f"{PUB_A}x"):
        assert parse_public_id(bad) is None


def test_scope_requires_an_explicit_choice() -> None:
    with pytest.raises(ValueError, match="pass tenant_id"):
        Scope()
    with pytest.raises(ValueError, match="cannot be combined"):
        Scope(tenant_id=1, all_rows=True)
    assert Scope.unscoped().all_rows is True
    assert Scope(community_id=3).community_id == 3


def test_resolve_in_scope_returns_numeric_id(dal: Any) -> None:
    expected = int(dal(dal.things.public_id == PUB_A).select().first().id)
    assert resolve_public_id_sync(dal, "things", PUB_A, scope=Scope(tenant_id=1)) == expected
    assert (
        resolve_public_id_sync(dal, "things", PUB_A.upper(), scope=Scope(community_id=10))
        == expected
    )
    assert (
        resolve_public_id_sync(dal, "things", PUB_A, scope=Scope(tenant_id=1, community_id=10))
        == expected
    )
    assert resolve_public_id_sync(dal, "globals", PUB_C, scope=Scope.unscoped()) is not None


@pytest.mark.parametrize(
    "public_id,scope",
    [
        (PUB_A, Scope(tenant_id=2)),  # another tenant's row
        (PUB_A, Scope(community_id=20)),  # another community's row
        (PUB_A, Scope(tenant_id=1, community_id=20)),  # one axis wrong is enough
        (str(uuid.uuid4()), Scope.unscoped()),  # unknown uuid
        ("not-a-uuid", Scope.unscoped()),  # malformed
        ("12", Scope.unscoped()),  # a numeric id is never accepted
    ],
)
def test_every_miss_is_the_same_none(dal: Any, public_id: str, scope: Scope) -> None:
    assert resolve_public_id_sync(dal, "things", public_id, scope=scope) is None


def test_scope_column_missing_on_table_fails_loud(dal: Any) -> None:
    with pytest.raises(ValueError, match="no tenant_id"):
        resolve_public_id_sync(dal, "globals", PUB_C, scope=Scope(tenant_id=1))


def test_unbound_or_public_id_less_table_fails_loud(dal: Any) -> None:
    with pytest.raises(LookupError, match="not bound"):
        resolve_public_id_sync(dal, "missing", PUB_A, scope=Scope.unscoped())
    with pytest.raises(LookupError, match="no public_id"):
        resolve_public_id_sync(dal, "legacy", PUB_A, scope=Scope.unscoped())


@pytest.mark.asyncio
async def test_async_resolve_native_and_threaded_agree(dal: Any) -> None:
    sync = resolve_public_id_sync(dal, "things", PUB_B, scope=Scope(tenant_id=2))
    native = await resolve_public_id(
        dal, "things", PUB_B, scope=Scope(tenant_id=2), async_dal=FakeAsyncDal()
    )
    threaded = await resolve_public_id(dal, "things", PUB_B, scope=Scope(tenant_id=2))
    assert sync is not None and native == sync == threaded
    for async_dal in (FakeAsyncDal(), None):
        assert (
            await resolve_public_id(
                dal, "things", PUB_B, scope=Scope(tenant_id=1), async_dal=async_dal
            )
            is None
        )
        assert (
            await resolve_public_id(
                dal, "things", "12", scope=Scope.unscoped(), async_dal=async_dal
            )
            is None
        )


@pytest.mark.asyncio
async def test_or_unknown_substitutes_the_sentinel(dal: Any) -> None:
    miss = await resolve_public_id_or_unknown(dal, "things", PUB_A, scope=Scope(tenant_id=2))
    assert miss == UNKNOWN_ID == -1
    hit = await resolve_public_id_or_unknown(dal, "things", PUB_A, scope=Scope(tenant_id=1))
    assert hit > 0


def test_lookup_public_ids_batches_and_fails_loud(dal: Any) -> None:
    ids = [int(r.id) for r in dal(dal.things.id > 0).select()]
    assert lookup_public_ids(dal, "things", ids + ids) == {ids[0]: PUB_A, ids[1]: PUB_B}
    assert lookup_public_ids(dal, "things", []) == {}
    with pytest.raises(RuntimeError, match="missing public_id"):
        lookup_public_ids(dal, "things", [*ids, 9999])
    dal(dal.things.id == ids[0]).update(public_id=None)
    dal.commit()
    with pytest.raises(RuntimeError, match="missing public_id"):
        lookup_public_ids(dal, "things", ids)


def _app(dal: Any, targets: dict[str, PublicIdTarget]) -> Quart:
    """App with one `<puuid:thing_id>` route under an int community, plus the translator."""
    register_default_converter()
    app = Quart(__name__)
    app.config["dal"] = dal
    app.before_request(build_view_arg_translator(targets))

    @app.route("/c/<int:community_id>/things/<puuid:thing_id>")
    async def get_thing(community_id: int, thing_id: int) -> Any:
        return {"thing_id": thing_id, "type": type(thing_id).__name__}

    @app.route("/g/<puuid:thing_id>")
    async def get_global(thing_id: int) -> Any:
        return {"thing_id": thing_id}

    return app


@pytest.mark.asyncio
async def test_converter_routes_only_uuids(dal: Any) -> None:
    app = _app(dal, {"thing_id": PublicIdTarget("things")})
    client = app.test_client()
    assert (await client.get("/c/10/things/12")).status_code == 404
    assert (await client.get("/c/10/things/not-a-uuid")).status_code == 404
    ok = await client.get(f"/c/10/things/{PUB_A.upper()}")
    assert ok.status_code == 200
    body = await ok.get_json()
    assert body["type"] == "int" and body["thing_id"] > 0


@pytest.mark.asyncio
async def test_translator_scopes_to_the_route_community(dal: Any) -> None:
    app = _app(dal, {"thing_id": PublicIdTarget("things")})
    client = app.test_client()
    real = (await (await client.get(f"/c/10/things/{PUB_A}")).get_json())["thing_id"]
    assert real == resolve_public_id_sync(dal, "things", PUB_A, scope=Scope(community_id=10))
    # another community's UUID, an unknown UUID and nonexistent/zero communities: all the sentinel
    for path in (
        f"/c/10/things/{PUB_B}",
        f"/c/10/things/{uuid.uuid4()}",
        f"/c/999/things/{PUB_A}",
        f"/c/0/things/{PUB_A}",
    ):
        body = await (await client.get(path)).get_json()
        assert body["thing_id"] == UNKNOWN_ID


@pytest.mark.asyncio
async def test_translator_unscoped_target_for_global_tables(dal: Any) -> None:
    app = _app(dal, {"thing_id": PublicIdTarget("globals", community_param=None)})
    body = await (await app.test_client().get(f"/g/{PUB_C}")).get_json()
    assert body["thing_id"] == resolve_public_id_sync(dal, "globals", PUB_C, scope=Scope.unscoped())


def test_public_id_marker_is_a_str() -> None:
    marker = PublicId(PUB_A)
    assert isinstance(marker, str) and marker == PUB_A
