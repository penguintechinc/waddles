"""Blueprint tests for POST/GET /api/v1/apps/{app_id}/versions."""

from __future__ import annotations

from io import BytesIO
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema
from werkzeug.datastructures import FileStorage

from blueprints.v1.bundle_versions import BLUEPRINTS
from services.bundle_version_service import BUNDLE_MAX_REQUEST_BYTES
from tests.conftest import make_token

_MANIFEST_YAML = b"""
schema_version: 2
app_id: waddles.socials.music.default
name: Music Station Song Request
version: 3.0.1
feature: waddles.socials.music
module: socials
provider: builtin
language: python
artifact: source
stages:
  process:
    entry: "bundles.social_music_process:transform"
    consumes:
      - platform: twitch
        event_types: ["chat.message"]
"""


def _upload_files(
    *, manifest: bytes | None = _MANIFEST_YAML, source: bytes | None = b"fake-tarball"
) -> dict[str, Any]:
    files: dict[str, Any] = {}
    if manifest is not None:
        files["manifest"] = FileStorage(BytesIO(manifest), filename="bundle.yaml")
    if source is not None:
        files["source"] = FileStorage(BytesIO(source), filename="source.tar.zst")
    return files


@pytest.fixture
def app(bundle_install_db: Any, install_dal: Any) -> Quart:
    app = Quart(__name__)
    QuartSchema(app)
    # Mirrors app.py::create_app()'s own MAX_CONTENT_LENGTH -- Quart's own
    # built-in default (16 MiB) sits exactly at BUNDLE_MAX_SOURCE_BYTES,
    # which would otherwise mask this blueprint's own oversize-part checks
    # for anything larger (in particular the 32 MiB component ceiling)
    # behind Quart's generic, non-JSON 413 instead of this route's own.
    app.config["MAX_CONTENT_LENGTH"] = BUNDLE_MAX_REQUEST_BYTES
    app.config["async_dal"] = bundle_install_db
    app.config["dal"] = bundle_install_db.dal
    app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)
    return app


async def test_post_version_requires_platform_admin_scope(app: Quart) -> None:
    token = make_token(scope="")
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.socials.music.default/versions",
        headers={"Authorization": f"Bearer {token}"},
        files=_upload_files(source=None),
    )
    assert response.status_code == 403


async def test_post_version_happy_path(app: Quart) -> None:
    token = make_token(scope="platform:admin", user_id="1")
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.socials.music.default/versions",
        headers={"Authorization": f"Bearer {token}"},
        files=_upload_files(),
    )
    assert response.status_code == 202
    body = await response.get_json()
    assert body["status"] == "UPLOADED"
    assert body["versionId"] is not None


async def test_post_version_missing_manifest_is_400(app: Quart) -> None:
    token = make_token(scope="platform:admin")
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.socials.music.default/versions",
        headers={"Authorization": f"Bearer {token}"},
        files=_upload_files(manifest=None),
    )
    assert response.status_code == 400


async def test_post_version_manifest_app_id_mismatching_the_url_is_400(app: Quart) -> None:
    """The URL says `forums`, the manifest says `music` -- refused before any DB write."""
    token = make_token(scope="platform:admin", user_id="1")
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.socials.forums.default/versions",
        headers={"Authorization": f"Bearer {token}"},
        files=_upload_files(),
    )
    assert response.status_code == 400
    body = await response.get_json()
    assert body["error"]["code"] == "app_id_mismatch"


async def test_post_version_oversize_source_is_413_without_a_500(app: Quart) -> None:
    """A source part over the 16 MiB ceiling is refused via a bounded read, not a full buffer."""
    token = make_token(scope="platform:admin", user_id="1")
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.socials.music.default/versions",
        headers={"Authorization": f"Bearer {token}"},
        files=_upload_files(source=b"x" * (16_777_216 + 1)),
    )
    assert response.status_code == 413
    body = await response.get_json()
    assert body["error"]["code"] == "PAYLOAD_TOO_LARGE"


async def test_get_version_returns_404_for_unknown_version(app: Quart) -> None:
    token = make_token(scope="")
    client = app.test_client()
    response = await client.get(
        "/api/v1/apps/waddles.socials.music.default/versions/9.9.9",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404


async def test_get_version_after_post_reflects_state(app: Quart) -> None:
    token = make_token(scope="platform:admin", user_id="1")
    client = app.test_client()
    await client.post(
        "/api/v1/apps/waddles.socials.music.default/versions",
        headers={"Authorization": f"Bearer {token}"},
        files=_upload_files(),
    )
    response = await client.get(
        "/api/v1/apps/waddles.socials.music.default/versions/3.0.1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["status"] == "UPLOADED"
    assert body["scanStatus"] is None
    assert body["artifactDigest"] is None


async def test_list_versions_route(app: Quart) -> None:
    token = make_token(scope="platform:admin", user_id="1")
    client = app.test_client()
    await client.post(
        "/api/v1/apps/waddles.socials.music.default/versions",
        headers={"Authorization": f"Bearer {token}"},
        files=_upload_files(),
    )
    response = await client.get(
        "/api/v1/apps/waddles.socials.music.default/versions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["versions"][0]["version"] == "3.0.1"
