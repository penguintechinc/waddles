"""Blueprint tests for `/api/v1/communities/<uuid>/ingest-sources` (ingest_sources registry).

Every id on the wire (community path segment, source path segment, DTO `id`/`communityId`,
list cursor) is a `public_id` UUID; the numeric surrogate keys never cross the boundary.

Not to be confused with `/api/v1/communities/<id>/connections/...`
(`blueprints/v1/community_connections.py`) -- the pre-existing, unrelated
per-community OAuth token feature. See this blueprint's own module
docstring for the full reconciliation note.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.ingest_sources import BLUEPRINTS
from tests.conftest import TENANT_SLUG, make_user_token


@pytest.fixture
async def app(bundle_install_db: Any, install_dal: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["async_dal"] = bundle_install_db
    quart_app.config["dal"] = bundle_install_db.dal
    quart_app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        quart_app.register_blueprint(bp)
    return quart_app


async def _make_community_ids(
    install_dal: Any, tenant_id: int, name: str = "acme-community"
) -> tuple[int, str]:
    """Insert a community; returns `(internal id, public_id UUID)`."""
    public_id = str(uuid.uuid4())
    internal = int(
        await install_dal.communities.async_insert(
            tenant_id=tenant_id, name=name, public_id=public_id
        )
    )
    return internal, public_id


async def _make_community(install_dal: Any, tenant_id: int, name: str = "acme-community") -> str:
    """Insert a community; returns its public UUID (the only id the API accepts)."""
    return (await _make_community_ids(install_dal, tenant_id, name))[1]


def _admin_token() -> str:
    #: real tenant-admin bundle grants both scopes (`libs/flask_core/flask_core/
    #: auth.py`'s SCOPE_BUNDLES['tenant']['admin']) -- tests that both write
    #: and immediately read back use this token, matching a real admin caller.
    return make_user_token(user_id=1, scope="tenant:admin tenant:read", tenant=TENANT_SLUG)


def _viewer_token() -> str:
    return make_user_token(user_id=1, scope="tenant:read", tenant=TENANT_SLUG)


async def test_create_requires_tenant_admin_scope(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _make_community(install_dal, tenant_id=1)
    response = await app.test_client().post(
        f"/api/v1/communities/{community_id}/ingest-sources",
        headers={"Authorization": f"Bearer {_viewer_token()}"},
        json={"platform": "discord", "sourceId": "guild-1", "label": "Main"},
    )
    assert response.status_code == 403


async def test_create_and_get_via_list(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _make_community(install_dal, tenant_id=1)
    headers = {"Authorization": f"Bearer {_admin_token()}"}
    response = await app.test_client().post(
        f"/api/v1/communities/{community_id}/ingest-sources",
        headers=headers,
        json={"platform": "discord", "sourceId": "guild-1", "label": "Main Discord"},
    )
    assert response.status_code == 201
    body = await response.get_json()
    assert body["ingestSource"]["platform"] == "discord"
    assert body["ingestSource"]["sourceId"] == "guild-1"
    assert body["ingestSource"]["communityId"] == community_id
    assert "secret" not in body["ingestSource"]
    assert "token" not in body["ingestSource"]

    list_response = await app.test_client().get(
        f"/api/v1/communities/{community_id}/ingest-sources", headers=headers
    )
    assert list_response.status_code == 200
    list_body = await list_response.get_json()
    assert len(list_body["ingestSources"]) == 1
    assert list_body["ingestSources"][0]["sourceId"] == "guild-1"


async def test_create_rejects_a_secret_field(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _make_community(install_dal, tenant_id=1)
    response = await app.test_client().post(
        f"/api/v1/communities/{community_id}/ingest-sources",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={
            "platform": "discord",
            "sourceId": "guild-1",
            "label": "Main",
            "secret": "sk-should-not-be-accepted",
        },
    )
    assert response.status_code == 400


async def test_create_rejects_a_token_field(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _make_community(install_dal, tenant_id=1)
    response = await app.test_client().post(
        f"/api/v1/communities/{community_id}/ingest-sources",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={
            "platform": "discord",
            "sourceId": "guild-1",
            "label": "Main",
            "token": "should-not-be-accepted",
        },
    )
    assert response.status_code == 400


async def test_create_rejects_a_community_id_body_field(app: Quart) -> None:
    """`communityId` comes from the path only -- a body field is rejected as an unknown key."""
    install_dal = app.config["install_dal"]
    community_id = await _make_community(install_dal, tenant_id=1)
    response = await app.test_client().post(
        f"/api/v1/communities/{community_id}/ingest-sources",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={
            "platform": "discord",
            "sourceId": "guild-1",
            "label": "Main",
            "communityId": community_id,
        },
    )
    assert response.status_code == 400


async def test_create_rejects_unsupported_platform(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _make_community(install_dal, tenant_id=1)
    response = await app.test_client().post(
        f"/api/v1/communities/{community_id}/ingest-sources",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={"platform": "myspace", "sourceId": "x", "label": "x"},
    )
    assert response.status_code == 422


async def test_create_duplicate_returns_409(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _make_community(install_dal, tenant_id=1)
    client = app.test_client()
    headers = {"Authorization": f"Bearer {_admin_token()}"}
    payload = {"platform": "discord", "sourceId": "guild-dup", "label": "a"}
    first = await client.post(
        f"/api/v1/communities/{community_id}/ingest-sources", headers=headers, json=payload
    )
    assert first.status_code == 201
    second = await client.post(
        f"/api/v1/communities/{community_id}/ingest-sources", headers=headers, json=payload
    )
    assert second.status_code == 409


async def test_create_same_source_in_second_community_returns_409(app: Quart) -> None:
    """Known gap (module docstring): migration 0020's real constraint is tenant-wide."""
    install_dal = app.config["install_dal"]
    community_a = await _make_community(install_dal, tenant_id=1, name="community-a")
    community_b = await _make_community(install_dal, tenant_id=1, name="community-b")
    client = app.test_client()
    headers = {"Authorization": f"Bearer {_admin_token()}"}
    payload = {"platform": "discord", "sourceId": "shared-guild", "label": "a"}
    first = await client.post(
        f"/api/v1/communities/{community_a}/ingest-sources", headers=headers, json=payload
    )
    assert first.status_code == 201
    second = await client.post(
        f"/api/v1/communities/{community_b}/ingest-sources", headers=headers, json=payload
    )
    assert second.status_code == 409
    body = await second.get_json()
    assert body["error"]["code"] == "SOURCE_LINKED_TO_ANOTHER_COMMUNITY"


async def test_create_community_not_in_tenant_returns_404(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    other_community_id = await _make_community(
        install_dal, tenant_id=999, name="other-tenant-community"
    )
    response = await app.test_client().post(
        f"/api/v1/communities/{other_community_id}/ingest-sources",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={"platform": "discord", "sourceId": "guild-1", "label": "Main"},
    )
    assert response.status_code == 404


async def test_list_community_not_in_tenant_returns_404(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    other_community_id = await _make_community(
        install_dal, tenant_id=999, name="other-tenant-community"
    )
    response = await app.test_client().get(
        f"/api/v1/communities/{other_community_id}/ingest-sources",
        headers={"Authorization": f"Bearer {_viewer_token()}"},
    )
    assert response.status_code == 404


async def test_list_filters_by_platform(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _make_community(install_dal, tenant_id=1)
    client = app.test_client()
    headers = {"Authorization": f"Bearer {_admin_token()}"}
    await client.post(
        f"/api/v1/communities/{community_id}/ingest-sources",
        headers=headers,
        json={"platform": "discord", "sourceId": "a", "label": "a"},
    )
    await client.post(
        f"/api/v1/communities/{community_id}/ingest-sources",
        headers=headers,
        json={"platform": "twitch", "sourceId": "b", "label": "b"},
    )
    response = await client.get(
        f"/api/v1/communities/{community_id}/ingest-sources?platform=twitch", headers=headers
    )
    body = await response.get_json()
    assert len(body["ingestSources"]) == 1
    assert body["ingestSources"][0]["platform"] == "twitch"


async def test_list_is_community_scoped(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_a = await _make_community(install_dal, tenant_id=1, name="community-a")
    community_b = await _make_community(install_dal, tenant_id=1, name="community-b")
    client = app.test_client()
    headers = {"Authorization": f"Bearer {_admin_token()}"}
    await client.post(
        f"/api/v1/communities/{community_a}/ingest-sources",
        headers=headers,
        json={"platform": "discord", "sourceId": "a", "label": "a"},
    )
    await client.post(
        f"/api/v1/communities/{community_b}/ingest-sources",
        headers=headers,
        json={"platform": "discord", "sourceId": "b", "label": "b"},
    )
    response = await client.get(
        f"/api/v1/communities/{community_a}/ingest-sources", headers=headers
    )
    body = await response.get_json()
    assert len(body["ingestSources"]) == 1
    assert body["ingestSources"][0]["sourceId"] == "a"


async def test_list_empty_result(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _make_community(install_dal, tenant_id=1)
    response = await app.test_client().get(
        f"/api/v1/communities/{community_id}/ingest-sources",
        headers={"Authorization": f"Bearer {_viewer_token()}"},
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["ingestSources"] == []
    assert body["meta"]["nextCursor"] is None


async def test_list_rejects_an_invalid_enabled_filter(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _make_community(install_dal, tenant_id=1)
    response = await app.test_client().get(
        f"/api/v1/communities/{community_id}/ingest-sources?enabled=maybe",
        headers={"Authorization": f"Bearer {_viewer_token()}"},
    )
    assert response.status_code == 422


async def test_list_paginates_via_cursor(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _make_community(install_dal, tenant_id=1)
    client = app.test_client()
    headers = {"Authorization": f"Bearer {_admin_token()}"}
    for i in range(3):
        await client.post(
            f"/api/v1/communities/{community_id}/ingest-sources",
            headers=headers,
            json={"platform": "discord", "sourceId": f"g-{i}", "label": f"g{i}"},
        )
    page1 = await client.get(
        f"/api/v1/communities/{community_id}/ingest-sources?limit=2", headers=headers
    )
    page1_body = await page1.get_json()
    assert len(page1_body["ingestSources"]) == 2
    cursor = page1_body["meta"]["nextCursor"]
    assert cursor is not None
    page2 = await client.get(
        f"/api/v1/communities/{community_id}/ingest-sources?limit=2&cursor={cursor}",
        headers=headers,
    )
    page2_body = await page2.get_json()
    assert len(page2_body["ingestSources"]) == 1


async def test_list_never_returns_another_tenants_sources(app: Quart) -> None:
    dal = app.config["dal"]
    other_tenant_id = int(
        dal.tenants.insert(slug="other-corp", display_name="Other", is_active=True)
    )
    dal.commit()
    install_dal = app.config["install_dal"]
    community_id = await _make_community(install_dal, tenant_id=1)
    other_community_id, _ = await _make_community_ids(install_dal, tenant_id=other_tenant_id)
    await install_dal.ingest_sources.async_insert(
        tenant_id=other_tenant_id,
        community_id=other_community_id,
        platform="discord",
        source_id="other-guild",
        label="other",
        secret_ciphertext=None,
        secret_iv=None,
        mapping=None,
        enabled=True,
    )
    response = await app.test_client().get(
        f"/api/v1/communities/{community_id}/ingest-sources",
        headers={"Authorization": f"Bearer {_viewer_token()}"},
    )
    body = await response.get_json()
    assert body["ingestSources"] == []


async def test_patch_disables_an_ingest_source(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _make_community(install_dal, tenant_id=1)
    client = app.test_client()
    headers = {"Authorization": f"Bearer {_admin_token()}"}
    created = await client.post(
        f"/api/v1/communities/{community_id}/ingest-sources",
        headers=headers,
        json={"platform": "discord", "sourceId": "a", "label": "a"},
    )
    ingest_source_id = (await created.get_json())["ingestSource"]["id"]
    response = await client.patch(
        f"/api/v1/communities/{community_id}/ingest-sources/{ingest_source_id}",
        headers=headers,
        json={"enabled": False},
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["ingestSource"]["enabled"] is False


async def test_patch_requires_tenant_admin_scope(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _make_community(install_dal, tenant_id=1)
    response = await app.test_client().patch(
        f"/api/v1/communities/{community_id}/ingest-sources/1",
        headers={"Authorization": f"Bearer {_viewer_token()}"},
        json={"enabled": False},
    )
    assert response.status_code == 403


async def test_patch_rejects_a_secret_field(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _make_community(install_dal, tenant_id=1)
    client = app.test_client()
    headers = {"Authorization": f"Bearer {_admin_token()}"}
    created = await client.post(
        f"/api/v1/communities/{community_id}/ingest-sources",
        headers=headers,
        json={"platform": "discord", "sourceId": "a", "label": "a"},
    )
    ingest_source_id = (await created.get_json())["ingestSource"]["id"]
    response = await client.patch(
        f"/api/v1/communities/{community_id}/ingest-sources/{ingest_source_id}",
        headers=headers,
        json={"secret": "sk-nope"},
    )
    assert response.status_code == 400


async def test_patch_missing_returns_404(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _make_community(install_dal, tenant_id=1)
    response = await app.test_client().patch(
        f"/api/v1/communities/{community_id}/ingest-sources/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={"label": "x"},
    )
    assert response.status_code == 404


async def test_patch_another_tenants_source_returns_404(app: Quart) -> None:
    dal = app.config["dal"]
    other_tenant_id = int(
        dal.tenants.insert(slug="other-corp-3", display_name="Other", is_active=True)
    )
    dal.commit()
    install_dal = app.config["install_dal"]
    community_id = await _make_community(install_dal, tenant_id=1)
    other_community_id, _ = await _make_community_ids(install_dal, tenant_id=other_tenant_id)
    other_id = await install_dal.ingest_sources.async_insert(
        tenant_id=other_tenant_id,
        community_id=other_community_id,
        platform="discord",
        source_id="other-guild-2",
        label="other",
        secret_ciphertext=None,
        secret_iv=None,
        mapping=None,
        enabled=True,
    )
    other_public_id = str(
        (await install_dal(install_dal.ingest_sources.id == other_id).select()).first().public_id
    )
    response = await app.test_client().patch(
        f"/api/v1/communities/{community_id}/ingest-sources/{other_public_id}",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={"label": "hijacked"},
    )
    assert response.status_code == 404


async def test_delete_removes_an_ingest_source(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _make_community(install_dal, tenant_id=1)
    client = app.test_client()
    headers = {"Authorization": f"Bearer {_admin_token()}"}
    created = await client.post(
        f"/api/v1/communities/{community_id}/ingest-sources",
        headers=headers,
        json={"platform": "discord", "sourceId": "a", "label": "a"},
    )
    ingest_source_id = (await created.get_json())["ingestSource"]["id"]
    response = await client.delete(
        f"/api/v1/communities/{community_id}/ingest-sources/{ingest_source_id}", headers=headers
    )
    assert response.status_code == 204

    list_response = await client.get(
        f"/api/v1/communities/{community_id}/ingest-sources", headers=headers
    )
    assert (await list_response.get_json())["ingestSources"] == []


async def test_delete_requires_tenant_admin_scope(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _make_community(install_dal, tenant_id=1)
    response = await app.test_client().delete(
        f"/api/v1/communities/{community_id}/ingest-sources/1",
        headers={"Authorization": f"Bearer {_viewer_token()}"},
    )
    assert response.status_code == 403


async def test_delete_missing_returns_404(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _make_community(install_dal, tenant_id=1)
    response = await app.test_client().delete(
        f"/api/v1/communities/{community_id}/ingest-sources/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {_admin_token()}"},
    )
    assert response.status_code == 404


# -- public_id-only boundary (numeric surrogate keys never cross the API) ---------------------

_DTO_FIELDS = {
    "id",
    "platform",
    "sourceId",
    "label",
    "enabled",
    "communityId",
    "createdAt",
    "updatedAt",
}


async def _create(app: Quart, community_public_id: str, source_id: str = "g-1") -> dict[str, Any]:
    response = await app.test_client().post(
        f"/api/v1/communities/{community_public_id}/ingest-sources",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={"platform": "discord", "sourceId": source_id, "label": "Main"},
    )
    assert response.status_code == 201
    body: dict[str, Any] = (await response.get_json())["ingestSource"]
    return body


async def test_dto_exact_field_set_and_uuid_ids(app: Quart) -> None:
    """regression: the DTO is exactly this field set; `id`/`communityId` are public UUIDs."""
    install_dal = app.config["install_dal"]
    community_internal, community_public = await _make_community_ids(install_dal, tenant_id=1)
    dto = await _create(app, community_public)
    assert set(dto) == _DTO_FIELDS
    assert str(uuid.UUID(dto["id"])) == dto["id"]
    assert dto["communityId"] == community_public
    assert dto["communityId"] != str(community_internal)
    row = (await install_dal(install_dal.ingest_sources.public_id == dto["id"]).select()).first()
    assert str(dto["id"]) != str(row.id)


async def test_list_dto_exact_field_set_and_cursor_is_a_uuid(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_public = await _make_community(install_dal, tenant_id=1)
    created = [await _create(app, community_public, f"g-{i}") for i in range(3)]
    headers = {"Authorization": f"Bearer {_admin_token()}"}
    page1 = await (
        await app.test_client().get(
            f"/api/v1/communities/{community_public}/ingest-sources?limit=2", headers=headers
        )
    ).get_json()
    assert all(set(item) == _DTO_FIELDS for item in page1["ingestSources"])
    cursor = page1["meta"]["nextCursor"]
    assert cursor == created[1]["id"]
    page2 = await (
        await app.test_client().get(
            f"/api/v1/communities/{community_public}/ingest-sources?limit=2&cursor={cursor}",
            headers=headers,
        )
    ).get_json()
    assert [i["id"] for i in page2["ingestSources"]] == [created[2]["id"]]
    assert page2["meta"]["nextCursor"] is None


@pytest.mark.parametrize("cursor", ["1", "not-a-uuid", str(uuid.UUID(int=7))])
async def test_list_rejects_a_numeric_or_unknown_cursor(app: Quart, cursor: str) -> None:
    install_dal = app.config["install_dal"]
    community_public = await _make_community(install_dal, tenant_id=1)
    await _create(app, community_public)
    response = await app.test_client().get(
        f"/api/v1/communities/{community_public}/ingest-sources?cursor={cursor}",
        headers={"Authorization": f"Bearer {_viewer_token()}"},
    )
    assert response.status_code == 422
    assert (await response.get_json())["error"]["code"] == "invalid_cursor"


@pytest.mark.parametrize("method", ["get", "post"])
async def test_numeric_community_id_in_path_is_404(app: Quart, method: str) -> None:
    install_dal = app.config["install_dal"]
    internal, _ = await _make_community_ids(install_dal, tenant_id=1)
    client = app.test_client()
    headers = {"Authorization": f"Bearer {_admin_token()}"}
    url = f"/api/v1/communities/{internal}/ingest-sources"
    if method == "get":
        response = await client.get(url, headers=headers)
    else:
        response = await client.post(
            url, headers=headers, json={"platform": "discord", "sourceId": "a", "label": "a"}
        )
    assert response.status_code == 404


@pytest.mark.parametrize("method", ["patch", "delete"])
async def test_numeric_source_id_in_path_is_404_and_changes_nothing(
    app: Quart, method: str
) -> None:
    install_dal = app.config["install_dal"]
    community_public = await _make_community(install_dal, tenant_id=1)
    dto = await _create(app, community_public)
    row = (await install_dal(install_dal.ingest_sources.public_id == dto["id"]).select()).first()
    client = app.test_client()
    headers = {"Authorization": f"Bearer {_admin_token()}"}
    url = f"/api/v1/communities/{community_public}/ingest-sources/{row.id}"
    if method == "patch":
        response = await client.patch(url, headers=headers, json={"label": "x"})
    else:
        response = await client.delete(url, headers=headers)
    assert response.status_code == 404
    still = (await install_dal(install_dal.ingest_sources.id == row.id).select()).first()
    assert still is not None and still.label == "Main"


async def test_uuid_routes_round_trip_patch_and_delete(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_public = await _make_community(install_dal, tenant_id=1)
    dto = await _create(app, community_public)
    client = app.test_client()
    headers = {"Authorization": f"Bearer {_admin_token()}"}
    url = f"/api/v1/communities/{community_public}/ingest-sources/{dto['id']}"
    patched = await client.patch(url, headers=headers, json={"label": "Renamed"})
    assert patched.status_code == 200
    body = (await patched.get_json())["ingestSource"]
    assert set(body) == _DTO_FIELDS and body["id"] == dto["id"] and body["label"] == "Renamed"
    assert (await client.delete(url, headers=headers)).status_code == 204
    assert (await client.delete(url, headers=headers)).status_code == 404


async def test_source_of_another_community_in_same_tenant_is_404(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_a = await _make_community(install_dal, tenant_id=1, name="community-a")
    community_b = await _make_community(install_dal, tenant_id=1, name="community-b")
    dto = await _create(app, community_a)
    response = await app.test_client().patch(
        f"/api/v1/communities/{community_b}/ingest-sources/{dto['id']}",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={"label": "hijacked"},
    )
    assert response.status_code == 404


async def test_cross_tenant_source_uuid_is_404_for_patch_delete_and_cursor(app: Quart) -> None:
    dal = app.config["dal"]
    other_tenant_id = int(
        dal.tenants.insert(slug="other-corp-uuid", display_name="O", is_active=True)
    )
    dal.commit()
    install_dal = app.config["install_dal"]
    community_public = await _make_community(install_dal, tenant_id=1)
    other_internal, other_public = await _make_community_ids(
        install_dal, tenant_id=other_tenant_id, name="other"
    )
    await install_dal.ingest_sources.async_insert(
        tenant_id=other_tenant_id,
        community_id=other_internal,
        platform="discord",
        source_id="other-guild-uuid",
        label="other",
        enabled=True,
    )
    other_row = (
        await install_dal(install_dal.ingest_sources.tenant_id == other_tenant_id).select()
    ).first()
    client = app.test_client()
    headers = {"Authorization": f"Bearer {_admin_token()}"}
    victim = str(other_row.public_id)
    # Source uuid under the caller's own community, and under the foreign community's uuid.
    for comm in (community_public, other_public):
        url = f"/api/v1/communities/{comm}/ingest-sources/{victim}"
        assert (await client.patch(url, headers=headers, json={"label": "x"})).status_code == 404
        assert (await client.delete(url, headers=headers)).status_code == 404
    cursor_resp = await client.get(
        f"/api/v1/communities/{community_public}/ingest-sources?cursor={victim}", headers=headers
    )
    assert cursor_resp.status_code == 422
    untouched = (await install_dal(install_dal.ingest_sources.id == other_row.id).select()).first()
    assert untouched is not None and untouched.label == "other"


pytestmark = pytest.mark.usefixtures("bypass_route_guards")
