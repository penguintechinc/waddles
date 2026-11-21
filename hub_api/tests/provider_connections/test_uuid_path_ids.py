"""`<puuid:connection_id>` on the provider-connection admin routes.

The connection's public UUID resolves through the community's grant: the owner's UUID works, a
UUID granted only to ANOTHER community (same tenant) is the identical 404 an unknown UUID gets,
and a numeric id in the path no longer routes.
"""

from __future__ import annotations

from typing import Any

from tests.provider_connections.conftest import admin_headers
from tests.provider_connections.test_api import UNKNOWN_CONN, _base, _create


async def _ids(client: Any, pc_db: Any, bot_tokens: Any) -> tuple[str, str]:
    created = await _create(client, pc_db, bot_tokens)
    assert created.status_code == 201
    body = (await created.get_json())["connection"]
    # the admin DTO's id IS the public UUID (never the numeric pk)
    assert len(body["id"]) == 36 and body["id"].count("-") == 4
    return body["id"], _base(pc_db)


async def test_owner_uuid_resolves_on_get_patch_delete(
    client: Any, pc_db: Any, bot_tokens: Any
) -> None:
    cid, base = await _ids(client, pc_db, bot_tokens)
    got = await client.get(f"{base}/{cid}", headers=admin_headers(pc_db))
    assert got.status_code == 200
    assert (await got.get_json())["connection"]["id"] == cid
    patched = await client.patch(
        f"{base}/{cid}", headers=admin_headers(pc_db), data='{"enabled": false}'
    )
    assert patched.status_code == 200
    deleted = await client.delete(f"{base}/{cid}", headers=admin_headers(pc_db))
    assert deleted.status_code in (200, 204)


async def test_foreign_community_uuid_is_the_same_404_as_unknown(
    client: Any, pc_db: Any, bot_tokens: Any
) -> None:
    cid, _ = await _ids(client, pc_db, bot_tokens)
    other = _base(pc_db, pc_db.seed.second_community_id)  # same tenant, NO grant on `cid`
    for method, kwargs in (("get", {}), ("patch", {"data": '{"enabled": true}'}), ("delete", {})):
        foreign = await getattr(client, method)(
            f"{other}/{cid}", headers=admin_headers(pc_db), **kwargs
        )
        unknown = await getattr(client, method)(
            f"{other}/{UNKNOWN_CONN}", headers=admin_headers(pc_db), **kwargs
        )
        assert foreign.status_code == unknown.status_code == 404, method
        bodies = [await foreign.get_json(), await unknown.get_json()]
        for body in bodies:
            body.get("error", {}).pop("timestamp", None)
        assert bodies[0] == bodies[1], method


async def test_numeric_connection_id_in_the_path_is_404(
    client: Any, pc_db: Any, bot_tokens: Any
) -> None:
    await _ids(client, pc_db, bot_tokens)
    base = _base(pc_db)
    for method, kwargs in (("get", {}), ("patch", {"data": '{"enabled": true}'}), ("delete", {})):
        response = await getattr(client, method)(
            f"{base}/1", headers=admin_headers(pc_db), **kwargs
        )
        assert response.status_code == 404, method
    discover = await client.post(f"{base}/1/discover", headers=admin_headers(pc_db))
    assert discover.status_code == 404
