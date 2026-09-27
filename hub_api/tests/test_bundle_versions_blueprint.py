"""Blueprint tests for POST/GET /api/v1/apps/{app_id}/versions."""

from __future__ import annotations

from io import BytesIO
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema
from werkzeug.datastructures import FileStorage

from blueprints.v1.bundle_versions import BLUEPRINTS
from services import bundle_version_service as svc
from services.bundle_component_validator import ComponentValidationResult
from services.bundle_version_service import BUNDLE_MAX_REQUEST_BYTES
from tests.conftest import make_token, make_user_token

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

#: A vendor's own namespace (`services/vendor_bundle_authz.py`) -- module
#: fixed to the existing `"integrations"` KNOWN_MODULES value, vendor id in
#: the feature-name segment (see that module's own docstring for why).
_VENDOR_COMPONENT_MANIFEST_YAML = b"""
schema_version: 2
app_id: waddles.integrations.vendor-42.mybundle
name: Vendor Bundle
version: 1.0.0
feature: waddles.integrations.vendor-42
module: integrations
provider: thirdparty
language: python
artifact: prebuilt
stages:
  process:
    consumes:
      - platform: twitch
        event_types: ["chat.message"]
  action: {}
"""


def _upload_files(
    *,
    manifest: bytes | None = _MANIFEST_YAML,
    source: bytes | None = b"fake-tarball",
    component: bytes | None = None,
) -> dict[str, Any]:
    files: dict[str, Any] = {}
    if manifest is not None:
        files["manifest"] = FileStorage(BytesIO(manifest), filename="bundle.yaml")
    if source is not None:
        files["source"] = FileStorage(BytesIO(source), filename="source.tar.zst")
    if component is not None:
        files["component"] = FileStorage(BytesIO(component), filename="bundle.wasm")
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


@pytest.fixture
def mock_component_pipeline(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Mocks the STAGE/PROVISION boundary (`validate_component`, MinIO, Valkey) for vendor tests.

    Defaults to a conformant component and a fresh consumer-group client;
    individual tests override `svc.validate_component`'s return value or
    the returned client's `xgroup_create.side_effect` for the negative
    cases.
    """
    monkeypatch.setattr(
        svc, "validate_component", AsyncMock(return_value=ComponentValidationResult(ok=True))
    )
    monkeypatch.setattr(
        svc.storage_service,
        "upload_bundle_component",
        AsyncMock(return_value="bundles/waddles.integrations.vendor-42.mybundle/1.0.0/abc.wasm"),
    )
    fake_client = AsyncMock()
    monkeypatch.setattr(svc.valkey_admin_client, "build_client", lambda: fake_client)
    return fake_client


def _vendor_component_files(manifest: bytes = _VENDOR_COMPONENT_MANIFEST_YAML) -> dict[str, Any]:
    return _upload_files(manifest=manifest, source=None, component=b"fake-wasm")


async def test_vendor_can_onboard_a_component_in_their_own_namespace(
    app: Quart, mock_component_pipeline: AsyncMock
) -> None:
    token = make_user_token(user_id=42, scope="vendor:onboard")
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.integrations.vendor-42.mybundle/versions",
        headers={"Authorization": f"Bearer {token}"},
        files=_vendor_component_files(),
    )
    assert response.status_code == 202
    body = await response.get_json()
    assert body["status"] == "ADDRESSING"
    assert mock_component_pipeline.xgroup_create.await_count == 2


async def test_vendor_cannot_onboard_the_core_namespace(
    app: Quart, mock_component_pipeline: AsyncMock
) -> None:
    token = make_user_token(user_id=42, scope="vendor:onboard")
    client = app.test_client()
    manifest = _VENDOR_COMPONENT_MANIFEST_YAML.replace(
        b"waddles.integrations.vendor-42.mybundle", b"waddles.core.socials.music"
    ).replace(b"feature: waddles.integrations.vendor-42", b"feature: waddles.core.socials")
    manifest = manifest.replace(b"module: integrations", b"module: core")
    response = await client.post(
        "/api/v1/apps/waddles.core.socials.music/versions",
        headers={"Authorization": f"Bearer {token}"},
        files=_vendor_component_files(manifest),
    )
    assert response.status_code == 403


async def test_vendor_cannot_onboard_another_vendors_namespace(
    app: Quart, mock_component_pipeline: AsyncMock
) -> None:
    token = make_user_token(user_id=42, scope="vendor:onboard")  # caller is vendor 42
    client = app.test_client()
    response = await client.post(
        # ...but the URL targets vendor 99's namespace
        "/api/v1/apps/waddles.integrations.vendor-99.other/versions",
        headers={"Authorization": f"Bearer {token}"},
        files=_vendor_component_files(),
    )
    assert response.status_code == 403


async def test_vendor_source_upload_is_rejected(
    app: Quart, mock_component_pipeline: AsyncMock
) -> None:
    token = make_user_token(user_id=42, scope="vendor:onboard")
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.integrations.vendor-42.mybundle/versions",
        headers={"Authorization": f"Bearer {token}"},
        files=_upload_files(
            manifest=_VENDOR_COMPONENT_MANIFEST_YAML, source=b"fake-tarball", component=None
        ),
    )
    assert response.status_code == 400
    body = await response.get_json()
    assert body["error"]["code"] == "vendor_source_not_supported"


async def test_vendor_with_no_scope_at_all_is_403(
    app: Quart, mock_component_pipeline: AsyncMock
) -> None:
    token = make_token(scope="")
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.integrations.vendor-42.mybundle/versions",
        headers={"Authorization": f"Bearer {token}"},
        files=_vendor_component_files(),
    )
    assert response.status_code == 403


async def test_nonconformant_component_is_202_with_rejected_status(
    app: Quart, mock_component_pipeline: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upload itself is accepted (202); the pipeline outcome surfaces via `status`."""
    rejected = ComponentValidationResult(ok=False, reason="disallowed_import:wasi:http/x")
    monkeypatch.setattr(svc, "validate_component", AsyncMock(return_value=rejected))
    token = make_user_token(user_id=42, scope="vendor:onboard")
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.integrations.vendor-42.mybundle/versions",
        headers={"Authorization": f"Bearer {token}"},
        files=_vendor_component_files(),
    )
    assert response.status_code == 202
    body = await response.get_json()
    assert body["status"] == "REJECTED"
    assert body["rejectReason"] is not None
    mock_component_pipeline.xgroup_create.assert_not_called()


async def test_reonboarding_a_second_version_tolerates_busygroup(
    app: Quart, mock_component_pipeline: AsyncMock
) -> None:
    """Two versions of one app_id both provision the same group -- BUSYGROUP on the 2nd."""
    import redis.exceptions

    call_count = 0

    async def _xgroup_create(*args: Any, **kwargs: Any) -> None:
        nonlocal call_count
        call_count += 1
        if call_count > 2:  # 1st version's 2 groups succeed; 2nd version's re-raise BUSYGROUP
            raise redis.exceptions.ResponseError("BUSYGROUP Consumer Group name already exists")

    mock_component_pipeline.xgroup_create.side_effect = _xgroup_create
    token = make_user_token(user_id=42, scope="vendor:onboard")
    client = app.test_client()

    for version in (b"1.0.0", b"1.0.1"):
        manifest = _VENDOR_COMPONENT_MANIFEST_YAML.replace(
            b"version: 1.0.0", b"version: " + version
        )
        response = await client.post(
            "/api/v1/apps/waddles.integrations.vendor-42.mybundle/versions",
            headers={"Authorization": f"Bearer {token}"},
            files=_vendor_component_files(manifest),
        )
        assert response.status_code == 202
        body = await response.get_json()
        assert body["status"] == "ADDRESSING"


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
