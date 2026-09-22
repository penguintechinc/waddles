"""Tests for bundle_version_service's create/get/list_versions() and advance_state()."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import yaml

from services.bundle_version_service import (
    STATUS_PUBLISHED,
    STATUS_REJECTED,
    STATUS_UPLOADED,
    STATUS_VALIDATING,
    advance_state,
    create_version,
    get_version,
    list_versions,
    valid_transition,
)
from services.errors import ApiError

_MANIFEST = {
    "schema_version": 2,
    "app_id": "waddles.socials.music.default",
    "name": "Music Station Song Request",
    "version": "3.0.1",
    "feature": "waddles.socials.music",
    "module": "socials",
    "provider": "builtin",
    "language": "python",
    "artifact": "source",
    "stages": {
        "process": {
            "entry": "bundles.social_music_process:transform",
            "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}],
        },
    },
}


async def test_create_version_happy_path(install_dal: Any) -> None:
    row = await create_version(
        install_dal,
        tenant_id=1,
        app_id="waddles.socials.music.default",
        requested_by=1,
        manifest_bytes=yaml.safe_dump(_MANIFEST).encode(),
        source_bytes=b"fake-tarball",
        component_bytes=None,
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )
    assert row.status == STATUS_UPLOADED
    assert row.version == "3.0.1"


async def test_create_version_rejects_duplicate(install_dal: Any) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="3.0.1",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status=STATUS_UPLOADED,
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(ApiError) as exc:
        await create_version(
            install_dal,
            tenant_id=1,
            app_id="waddles.socials.music.default",
            requested_by=1,
            manifest_bytes=yaml.safe_dump(_MANIFEST).encode(),
            source_bytes=b"x",
            component_bytes=None,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
        )
    assert exc.value.status_code == 409


async def test_create_version_rejects_bad_manifest(install_dal: Any) -> None:
    bad_manifest = {**_MANIFEST, "schema_version": 1}
    with pytest.raises(ApiError) as exc:
        await create_version(
            install_dal,
            tenant_id=1,
            app_id="waddles.socials.music.default",
            requested_by=1,
            manifest_bytes=yaml.safe_dump(bad_manifest).encode(),
            source_bytes=b"x",
            component_bytes=None,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
        )
    assert exc.value.status_code == 400
    assert exc.value.code == "unsupported_schema_version"


async def test_create_version_rejects_oversize_source(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await create_version(
            install_dal,
            tenant_id=1,
            app_id="waddles.socials.music.default",
            requested_by=1,
            manifest_bytes=yaml.safe_dump(_MANIFEST).encode(),
            source_bytes=b"x" * (16_777_216 + 1),
            component_bytes=None,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
        )
    assert exc.value.status_code == 413


async def test_create_version_rejects_prebuilt_when_disallowed(install_dal: Any) -> None:
    prebuilt_manifest = {**_MANIFEST, "artifact": "prebuilt", "language": "other"}
    del prebuilt_manifest["stages"]["process"]["entry"]
    with pytest.raises(ApiError) as exc:
        await create_version(
            install_dal,
            tenant_id=1,
            app_id="waddles.socials.music.default",
            requested_by=1,
            manifest_bytes=yaml.safe_dump(prebuilt_manifest).encode(),
            source_bytes=None,
            component_bytes=b"x",
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=False,
        )
    assert exc.value.status_code == 403
    assert exc.value.code == "prebuilt_not_allowed"


async def test_create_version_rejects_missing_source_part(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await create_version(
            install_dal,
            tenant_id=1,
            app_id="waddles.socials.music.default",
            requested_by=1,
            manifest_bytes=yaml.safe_dump(_MANIFEST).encode(),
            source_bytes=None,
            component_bytes=None,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
        )
    assert exc.value.status_code == 400
    assert exc.value.code == "missing_part"


async def test_get_version_not_found_raises_404(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await get_version(install_dal, app_id="waddles.x.y.default", version="1.0.0")
    assert exc.value.status_code == 404


async def test_list_versions_returns_all_versions_for_app_id(install_dal: Any) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="1.0.0",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status=STATUS_UPLOADED,
        created_at=now,
        updated_at=now,
    )
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="2.0.0",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status=STATUS_UPLOADED,
        created_at=now,
        updated_at=now,
    )
    rows = await list_versions(install_dal, app_id="waddles.socials.music.default")
    assert {r.version for r in rows} == {"1.0.0", "2.0.0"}


def test_valid_transition_uploaded_to_validating() -> None:
    assert valid_transition(STATUS_UPLOADED, STATUS_VALIDATING) is True


def test_valid_transition_rejects_skipping_states() -> None:
    assert valid_transition(STATUS_UPLOADED, STATUS_PUBLISHED) is False


def test_valid_transition_terminal_states_have_no_edges() -> None:
    assert valid_transition(STATUS_PUBLISHED, STATUS_VALIDATING) is False
    assert valid_transition(STATUS_REJECTED, STATUS_VALIDATING) is False


async def test_advance_state_moves_a_row_forward(install_dal: Any) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="1.0.0",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status=STATUS_UPLOADED,
        created_at=now,
        updated_at=now,
    )
    row = await advance_state(
        install_dal,
        app_id="waddles.socials.music.default",
        version="1.0.0",
        target=STATUS_VALIDATING,
    )
    assert row.status == STATUS_VALIDATING


async def test_advance_state_refuses_an_illegal_transition(install_dal: Any) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="1.0.0",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status=STATUS_UPLOADED,
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(ApiError) as exc:
        await advance_state(
            install_dal,
            app_id="waddles.socials.music.default",
            version="1.0.0",
            target=STATUS_PUBLISHED,
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "invalid_state_transition"


async def test_advance_state_unknown_version_raises_404(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await advance_state(
            install_dal, app_id="waddles.x.y.default", version="9.9.9", target=STATUS_VALIDATING
        )
    assert exc.value.status_code == 404
