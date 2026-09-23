"""Blueprint tests for GET permissions / POST approve / POST deny."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.bundle_approvals import BLUEPRINTS
from tests.conftest import make_token

_MANIFEST = {
    "schema_version": 2,
    "app_id": "waddles.socials.music.default",
    "name": "Music Station",
    "version": "3.0.1",
    "feature": "waddles.socials.music",
    "module": "socials",
    "provider": "builtin",
    "language": "python",
    "artifact": "source",
    "stages": {
        "process": {
            "entry": "x:y",
            "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}],
        }
    },
}


@pytest.fixture
async def app(bundle_install_db: Any, install_dal: Any) -> Quart:
    now = datetime.now(UTC)
    version_id = await install_dal.app_versions.async_insert(
        app_id="waddles.socials.music.default",
        version="3.0.1",
        artifact_digest="sha256:" + "a" * 64,
        language="python",
        artifact_kind="source",
        scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="3.0.1",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="PUBLISHED",
        manifest_json=_MANIFEST,
        app_version_id=version_id,
        created_at=now,
        updated_at=now,
    )
    # A second, non-terminal version -- deny is only legal off a
    # non-terminal state (spec Sec9.1); 3.0.1 above is PUBLISHED
    # (terminal) so the approve/permissions tests above have a
    # publishable row, this one is for the deny-happy-path test below.
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="1.0.0",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="UPLOADED",
        manifest_json={**_MANIFEST, "version": "1.0.0"},
        created_at=now,
        updated_at=now,
    )
    app = Quart(__name__)
    QuartSchema(app)
    app.config["async_dal"] = bundle_install_db
    app.config["dal"] = bundle_install_db.dal
    app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)
    return app


async def test_get_permissions_requires_platform_admin(app: Quart) -> None:
    token = make_token(scope="")
    client = app.test_client()
    response = await client.get(
        "/api/v1/apps/waddles.socials.music.default/versions/3.0.1/permissions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


async def test_get_permissions_happy_path(app: Quart) -> None:
    token = make_token(scope="platform:admin")
    client = app.test_client()
    response = await client.get(
        "/api/v1/apps/waddles.socials.music.default/versions/3.0.1/permissions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["permissionHash"].startswith("sha256:")


async def test_approve_mismatched_hash_fails_closed(app: Quart) -> None:
    token = make_token(scope="platform:admin", user_id="1")
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.socials.music.default/versions/3.0.1/approve",
        headers={"Authorization": f"Bearer {token}"},
        json={"communityId": None, "permissionHash": "sha256:" + "0" * 64},
    )
    assert response.status_code == 409
    body = await response.get_json()
    assert body["error"]["code"] == "permission_hash_mismatch"


async def test_approve_without_a_hash_succeeds_interactively(app: Quart) -> None:
    token = make_token(scope="platform:admin", user_id="1")
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.socials.music.default/versions/3.0.1/approve",
        headers={"Authorization": f"Bearer {token}"},
        json={"communityId": None, "permissionHash": None},
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["permissionHash"].startswith("sha256:")


async def test_deny_happy_path(app: Quart) -> None:
    """Deny succeeds from a non-terminal state -- 1.0.0 (UPLOADED), not 3.0.1 (PUBLISHED)."""
    token = make_token(scope="platform:admin")
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.socials.music.default/versions/1.0.0/deny",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "egress host not acceptable"},
    )
    assert response.status_code == 200


async def test_deny_on_a_published_version_is_409(app: Quart) -> None:
    """PUBLISHED is terminal (spec Sec9.1) -- deny must refuse, not silently reject it anyway."""
    token = make_token(scope="platform:admin")
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.socials.music.default/versions/3.0.1/deny",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "late objection"},
    )
    assert response.status_code == 409
    body = await response.get_json()
    assert body["error"]["code"] == "invalid_state_transition"


async def test_deny_requires_platform_admin(app: Quart) -> None:
    token = make_token(scope="")
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.socials.music.default/versions/1.0.0/deny",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "nope"},
    )
    assert response.status_code == 403
