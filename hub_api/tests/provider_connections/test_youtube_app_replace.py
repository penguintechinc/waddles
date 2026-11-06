"""Replacing a tenant's Google OAuth application: explicit, tenant-admin-only, and it kills the old.

A wrongly (or maliciously) installed app must be replaceable -- but only by a tenant admin who
confirms it, and replacing it must expire every connection minted under the revoked application,
dropping their sealed refresh tokens, with a secret-free audit line.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from flask_core.service_jwt import ServiceJwtIssuer

from tests.provider_connections.conftest import admin_headers
from tests.provider_connections.fake_google import APP_CLIENT_ID, FakeGoogle
from tests.provider_connections.support import raw_rows, set_column
from tests.provider_connections.test_api import _bound
from tests.provider_connections.test_youtube_flow import (
    INTERNAL,
    _connection_id,
    _install,
    _meta,
    _ok,
)

URL = "/api/v1/tenants/youtube/app"
NEW_APP = {"client_id": "new-app.apps.googleusercontent.com", "client_secret": "new-app-secret"}


def _tenant_admin(db: Any) -> dict[str, str]:
    return admin_headers(db, scope="tenant:admin")


async def _put(client: Any, headers: dict[str, str], body: dict[str, Any]) -> Any:
    return await client.put(URL, headers=headers, data=json.dumps(body))


def _app_row(db: Any) -> Any:
    db.dal.rollback()
    table = db.dal.tenant_platform_apps
    return (
        db.dal((table.tenant_id == db.seed.tenant_id) & (table.platform == "youtube"))
        .select()
        .first()
    )


async def _connected(client: Any, db: Any, google: FakeGoogle) -> int:
    assert await _ok((await _install(client, db, google))[0])
    return await _connection_id(client, db)


async def test_replacing_without_explicit_confirmation_is_refused_and_changes_nothing(
    yt_client: Any, yt_db: Any, fake_google: FakeGoogle
) -> None:
    await _connected(yt_client, yt_db, fake_google)
    before = _app_row(yt_db).credentials_ciphertext
    response = await _put(yt_client, _tenant_admin(yt_db), NEW_APP)
    assert response.status_code == 409
    assert (await response.get_json())["error"]["code"] == "YOUTUBE_APP_CONFLICT"
    explicit_false = await _put(yt_client, _tenant_admin(yt_db), {**NEW_APP, "replace": False})
    assert explicit_false.status_code == 409
    assert _app_row(yt_db).credentials_ciphertext == before
    (row,) = await raw_rows(yt_db, "platform_connections")
    assert row["status"] == "active" and row["refresh_token"] is not None


async def test_a_community_admin_cannot_replace_even_with_the_flag(
    yt_client: Any, yt_db: Any, fake_google: FakeGoogle
) -> None:
    await _connected(yt_client, yt_db, fake_google)
    before = _app_row(yt_db).credentials_ciphertext
    community_only = admin_headers(yt_db, scope="connections:admin")
    response = await _put(yt_client, community_only, {**NEW_APP, "replace": True})
    assert response.status_code == 403
    assert _app_row(yt_db).credentials_ciphertext == before
    (row,) = await raw_rows(yt_db, "platform_connections")
    assert row["status"] == "active"


async def test_a_tenant_admin_replace_expires_the_old_connections_and_drops_their_refresh_tokens(
    yt_client: Any,
    yt_db: Any,
    fake_google: FakeGoogle,
    service_issuer: ServiceJwtIssuer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    connection_id = await _connected(yt_client, yt_db, fake_google)
    (before,) = await raw_rows(yt_db, "platform_connections")
    assert before["refresh_token"] is not None
    with caplog.at_level(logging.INFO):
        response = await _put(yt_client, _tenant_admin(yt_db), {**NEW_APP, "replace": True})
    assert response.status_code == 200
    assert await response.get_json() == {"success": True, "invalidatedConnections": 1}

    row = _app_row(yt_db)  # the new app is stored, sealed
    assert NEW_APP["client_secret"] not in row.credentials_ciphertext
    (after,) = await raw_rows(yt_db, "platform_connections")
    assert (after["status"], after["status_reason"]) == ("expired", "app_replaced")
    assert after["refresh_token"] is None  # nothing can be refreshed any more
    assert after["id"] == before["id"]  # kept, so the admin can reconnect

    resolved = await yt_client.post(
        f"{INTERNAL}/communities/{yt_db.seed.community_id}/provider-connections/"
        f"{connection_id}/resolve",
        headers=_bound(service_issuer, yt_db.seed.community_id, connection_id),
    )
    assert resolved.status_code == 409
    assert (await resolved.get_json())["reason"] == "app_replaced"

    listed = await yt_client.get(
        f"/api/v1/communities/{yt_db.seed.community_id}/provider-connections",
        headers=admin_headers(yt_db),
    )
    (view,) = (await listed.get_json())["connections"]
    assert view["usable"] is False and view["hasRefreshToken"] is False

    audit = [
        r.getMessage() for r in caplog.records if "audit youtube_app_replaced" in r.getMessage()
    ]
    assert len(audit) == 1
    assert f"tenant_id={yt_db.seed.tenant_id}" in audit[0]
    assert f"actor_id={yt_db.seed.admin_user_id}" in audit[0]
    assert "invalidated=1" in audit[0]
    for secret in (NEW_APP["client_secret"], NEW_APP["client_id"], APP_CLIENT_ID):
        assert secret not in caplog.text


async def test_only_connections_minted_under_the_replaced_app_are_invalidated(
    yt_client: Any, yt_db: Any, fake_google: FakeGoogle
) -> None:
    await _connected(yt_client, yt_db, fake_google)
    (row,) = await raw_rows(yt_db, "platform_connections")
    facts = {**_meta(row), "client_id": "some-other-app.apps.googleusercontent.com"}
    await set_column(yt_db, "platform_connections", row["id"], resource_metadata=json.dumps(facts))
    response = await _put(yt_client, _tenant_admin(yt_db), {**NEW_APP, "replace": True})
    assert (await response.get_json())["invalidatedConnections"] == 0
    (after,) = await raw_rows(yt_db, "platform_connections")
    assert after["status"] == "active" and after["refresh_token"] is not None


async def test_first_write_and_same_client_rotation_need_no_confirmation(
    yt_client: Any, yt_db: Any, fake_google: FakeGoogle
) -> None:
    await _connected(yt_client, yt_db, fake_google)
    rotated = await _put(
        yt_client, _tenant_admin(yt_db), {"client_id": APP_CLIENT_ID, "client_secret": "rotated"}
    )
    assert (rotated.status_code, (await rotated.get_json())["invalidatedConnections"]) == (200, 0)
    (row,) = await raw_rows(yt_db, "platform_connections")
    assert row["status"] == "active"


async def test_the_global_tenant_can_never_replace(yt_client: Any, yt_db: Any) -> None:
    headers = admin_headers(
        yt_db, scope="tenant:admin", tenant="global", user_id=yt_db.global_admin_id
    )
    response = await _put(yt_client, headers, {**NEW_APP, "replace": True})
    assert response.status_code == 422
