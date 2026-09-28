"""Tests for `services.bundle_permission_service` -- the 3-tier grant storage and consent flow."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services import bundle_permission_service as svc
from services.bundle_manifest_v2 import parse_bundle_manifest_v2
from services.errors import ApiError

_APP_ID = "waddles.socials.music.default"
_CORE_APP_ID = "waddles.core.example.echo"
_VERSION = "3.0.0"

_MANIFEST_RAW = {
    "schema_version": 2,
    "app_id": _APP_ID,
    "name": "Music Station",
    "version": _VERSION,
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
    "permissions": [
        {"id": "storage.kv", "justification": "Stores state."},
        {"id": "ai.generate", "justification": "Generates a shoutout line."},
    ],
}


class _FakeValkeyClient:
    """Records every `xadd()` call -- no real Valkey connection in a unit test."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, str]]] = []

    async def xadd(self, stream: str, fields: dict[str, str]) -> None:
        self.published.append((stream, fields))

    async def aclose(self) -> None:
        return None


def _manifest() -> Any:
    return parse_bundle_manifest_v2(
        _MANIFEST_RAW,
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )


async def _seed_upload(install_dal: Any, *, app_id: str = _APP_ID, version: str = _VERSION) -> None:
    now = datetime.now(UTC)
    manifest_json = {**_MANIFEST_RAW, "app_id": app_id, "version": version}
    await install_dal.app_version_uploads.async_insert(
        app_id=app_id,
        version=version,
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="PUBLISHED",
        manifest_json=manifest_json,
        created_at=now,
        updated_at=now,
    )
    # `app_permission_requests.app_id` FKs to `app_catalog.app_id` in real
    # Postgres, but sqlite (this fixture's backend) does not enforce FKs
    # by default, so no `app_catalog` row is needed for these tests.


def test_classify_permission_diff_initial() -> None:
    assert svc.classify_permission_diff(frozenset({"storage.kv"}), None) == "initial"


def test_classify_permission_diff_unchanged() -> None:
    ids = frozenset({"storage.kv", "ai.generate"})
    assert svc.classify_permission_diff(ids, ids) == "unchanged"


def test_classify_permission_diff_narrowed() -> None:
    old = frozenset({"storage.kv", "ai.generate"})
    new = frozenset({"storage.kv"})
    assert svc.classify_permission_diff(new, old) == "narrowed"


def test_classify_permission_diff_widened_on_add() -> None:
    old = frozenset({"storage.kv"})
    new = frozenset({"storage.kv", "ai.generate"})
    assert svc.classify_permission_diff(new, old) == "widened"


def test_classify_permission_diff_mixed_is_widened() -> None:
    old = frozenset({"storage.kv"})
    new = frozenset({"ai.generate"})
    assert svc.classify_permission_diff(new, old) == "widened"


def test_requires_reconsent_true_on_add() -> None:
    assert svc.requires_reconsent(frozenset({"a", "b"}), frozenset({"a"})) is True


def test_requires_reconsent_false_on_narrow_or_unchanged() -> None:
    assert svc.requires_reconsent(frozenset({"a"}), frozenset({"a", "b"})) is False
    assert svc.requires_reconsent(frozenset({"a"}), frozenset({"a"})) is False


async def test_record_permission_requests_requires_dangerous_ack(install_dal: Any) -> None:
    await _seed_upload(install_dal)
    manifest = _manifest()
    with pytest.raises(ApiError) as exc:
        await svc.record_permission_requests(
            install_dal,
            app_id=_APP_ID,
            version=_VERSION,
            declarations=manifest.permission_declarations,
            approved_by=1,
            approved_permissions=frozenset(),
        )
    assert exc.value.code == "incomplete_dangerous_ack"


async def test_record_permission_requests_happy_path(install_dal: Any) -> None:
    await _seed_upload(install_dal)
    manifest = _manifest()
    await svc.record_permission_requests(
        install_dal,
        app_id=_APP_ID,
        version=_VERSION,
        declarations=manifest.permission_declarations,
        approved_by=1,
        approved_permissions=frozenset({"ai.generate"}),
    )
    ids = await svc.get_approved_permission_ids(install_dal, app_id=_APP_ID, version=_VERSION)
    assert ids == frozenset({"storage.kv", "ai.generate"})


async def test_record_permission_requests_system_source_rejects_non_core_app(
    install_dal: Any,
) -> None:
    await _seed_upload(install_dal)
    manifest = _manifest()
    with pytest.raises(ApiError) as exc:
        await svc.record_permission_requests(
            install_dal,
            app_id=_APP_ID,
            version=_VERSION,
            declarations=manifest.permission_declarations,
            approved_by=None,
            approval_source="system:core-seeder",
        )
    assert exc.value.status_code == 403


async def test_seed_core_permission_requests_skips_ack(install_dal: Any) -> None:
    await _seed_upload(install_dal, app_id=_CORE_APP_ID)
    manifest_raw = {
        **_MANIFEST_RAW,
        "app_id": _CORE_APP_ID,
        "feature": "waddles.core.example",
        "module": "core",
    }
    manifest = parse_bundle_manifest_v2(
        manifest_raw,
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )
    await svc.seed_core_permission_requests(
        install_dal, app_id=_CORE_APP_ID, version=_VERSION, manifest=manifest
    )
    ids = await svc.get_approved_permission_ids(install_dal, app_id=_CORE_APP_ID, version=_VERSION)
    assert ids == frozenset({"storage.kv", "ai.generate"})


async def _approve(install_dal: Any, *, app_id: str = _APP_ID, version: str = _VERSION) -> None:
    manifest_raw = {**_MANIFEST_RAW, "app_id": app_id, "version": version}
    manifest = parse_bundle_manifest_v2(
        manifest_raw,
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )
    await svc.record_permission_requests(
        install_dal,
        app_id=app_id,
        version=version,
        declarations=manifest.permission_declarations,
        approved_by=1,
        approved_permissions=frozenset({"ai.generate"}),
    )


async def test_restrict_tenant_permissions_rejects_outside_catalog(install_dal: Any) -> None:
    await _seed_upload(install_dal)
    await _approve(install_dal)
    with pytest.raises(ApiError) as exc:
        await svc.restrict_tenant_permissions(
            install_dal,
            tenant_id=1,
            app_id=_APP_ID,
            version=_VERSION,
            restricted_permission_ids=frozenset({"reputation.tenant.write"}),
            restricted_by=1,
        )
    assert exc.value.code == "permission_not_in_catalog_grant"


async def test_restrict_tenant_permissions_happy_path(install_dal: Any) -> None:
    await _seed_upload(install_dal)
    await _approve(install_dal)
    await svc.restrict_tenant_permissions(
        install_dal,
        tenant_id=1,
        app_id=_APP_ID,
        version=_VERSION,
        restricted_permission_ids=frozenset({"ai.generate"}),
        restricted_by=1,
    )
    restricted = await svc.get_tenant_restricted_ids(install_dal, tenant_id=1, app_id=_APP_ID)
    assert restricted == frozenset({"ai.generate"})


async def test_grant_community_permissions_consent_required(install_dal: Any) -> None:
    await _seed_upload(install_dal)
    await _approve(install_dal)
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme")
    manifest = _manifest()
    with pytest.raises(ApiError) as exc:
        await svc.grant_community_permissions(
            install_dal,
            tenant_id=1,
            community_id=community_id,
            app_id=_APP_ID,
            version=_VERSION,
            manifest=manifest,
            granted_permission_ids=frozenset({"storage.kv"}),  # missing ai.generate
            params_by_id=None,
            granted_by=1,
        )
    assert exc.value.code == "consent_required"


async def test_grant_community_permissions_rejects_restricted(install_dal: Any) -> None:
    await _seed_upload(install_dal)
    await _approve(install_dal)
    await svc.restrict_tenant_permissions(
        install_dal,
        tenant_id=1,
        app_id=_APP_ID,
        version=_VERSION,
        restricted_permission_ids=frozenset({"ai.generate"}),
        restricted_by=1,
    )
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme")
    manifest = _manifest()
    with pytest.raises(ApiError) as exc:
        await svc.grant_community_permissions(
            install_dal,
            tenant_id=1,
            community_id=community_id,
            app_id=_APP_ID,
            version=_VERSION,
            manifest=manifest,
            granted_permission_ids=frozenset({"storage.kv", "ai.generate"}),
            params_by_id=None,
            granted_by=1,
        )
    assert exc.value.code == "permission_not_in_catalog_grant"


async def test_grant_community_permissions_happy_path_bumps_version_and_publishes(
    install_dal: Any,
) -> None:
    await _seed_upload(install_dal)
    await _approve(install_dal)
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme")
    manifest = _manifest()
    fake_client = _FakeValkeyClient()
    grant_version = await svc.grant_community_permissions(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id=_APP_ID,
        version=_VERSION,
        manifest=manifest,
        granted_permission_ids=frozenset({"storage.kv", "ai.generate"}),
        params_by_id=None,
        granted_by=1,
        valkey_client=fake_client,
    )
    assert grant_version == 1
    granted = await svc.get_community_granted_ids(
        install_dal, community_id=community_id, app_id=_APP_ID
    )
    assert granted == frozenset({"storage.kv", "ai.generate"})
    assert len(fake_client.published) == 1
    stream, fields = fake_client.published[0]
    assert stream == svc.GRANT_INVALIDATION_STREAM
    assert fields["grant_version"] == "1"
    assert fields["app_id"] == _APP_ID


async def test_deactivate_permission_bumps_version_again(install_dal: Any) -> None:
    await _seed_upload(install_dal)
    await _approve(install_dal)
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme")
    manifest = _manifest()
    fake_client = _FakeValkeyClient()
    await svc.grant_community_permissions(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id=_APP_ID,
        version=_VERSION,
        manifest=manifest,
        granted_permission_ids=frozenset({"storage.kv", "ai.generate"}),
        params_by_id=None,
        granted_by=1,
        valkey_client=fake_client,
    )
    new_version = await svc.deactivate_permission(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id=_APP_ID,
        version=_VERSION,
        permission_id="ai.generate",
        deactivated_by=1,
        valkey_client=fake_client,
    )
    assert new_version == 2
    remaining = await svc.get_community_granted_ids(
        install_dal, community_id=community_id, app_id=_APP_ID
    )
    assert remaining == frozenset({"storage.kv"})
    assert len(fake_client.published) == 2


async def test_deactivate_permission_unknown_grant_is_404(install_dal: Any) -> None:
    await _seed_upload(install_dal)
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme")
    with pytest.raises(ApiError) as exc:
        await svc.deactivate_permission(
            install_dal,
            tenant_id=1,
            community_id=community_id,
            app_id=_APP_ID,
            version=_VERSION,
            permission_id="ai.generate",
            deactivated_by=1,
        )
    assert exc.value.status_code == 404


async def test_check_upgrade_reconsent_blocks_on_new_permission(install_dal: Any) -> None:
    await _seed_upload(install_dal, version="1.0.0")
    manifest_v1_raw = {
        **_MANIFEST_RAW,
        "version": "1.0.0",
        "permissions": [{"id": "storage.kv", "justification": "Stores state."}],
    }
    manifest_v1 = parse_bundle_manifest_v2(
        manifest_v1_raw,
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )
    await svc.record_permission_requests(
        install_dal,
        app_id=_APP_ID,
        version="1.0.0",
        declarations=manifest_v1.permission_declarations,
        approved_by=1,
    )
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme")
    fake_client = _FakeValkeyClient()
    await svc.grant_community_permissions(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id=_APP_ID,
        version="1.0.0",
        manifest=manifest_v1,
        granted_permission_ids=frozenset({"storage.kv"}),
        params_by_id=None,
        granted_by=1,
        valkey_client=fake_client,
    )

    await _seed_upload(install_dal, version="2.0.0")
    await _approve(install_dal, version="2.0.0")  # adds ai.generate

    may_auto_upgrade = await svc.check_upgrade_reconsent(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id=_APP_ID,
        old_version="1.0.0",
        new_version="2.0.0",
    )
    assert may_auto_upgrade is False
