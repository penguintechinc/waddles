"""Tests for get_permission_summary()/classify_diff()/deny_version() and the GLOBAL install tier.

GLOBAL tier: `install_version_globally()`/`uninstall_globally()`/
`list_global_installs()` -- the first of the App Bundle 3-tier split (see
`services/bundle_approval_service.py`'s own module docstring). TENANT tier
tests live in `test_tenant_app_availability_service.py`; COMMUNITY tier
(activation, AUTO-BIND, `routes_to`) tests live in
`test_bundle_activation_service.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.bundle_approval_service import (
    KV_PERMISSION_ID,
    _derive_capabilities,
    classify_diff,
    deny_version,
    get_permission_summary,
    install_version_globally,
    list_global_installs,
    uninstall_globally,
)
from services.bundle_manifest_v2 import parse_bundle_manifest_v2
from services.errors import ApiError
from services.tenant_app_availability_service import set_available

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
        },
    },
    "egress": [{"host": "api.spotify.com"}],
    "data": {"tables": ["music_queue"]},
}


async def _seed_published(install_dal: Any, *, manifest: dict = _MANIFEST) -> None:
    now = datetime.now(UTC)
    version_id = await install_dal.app_versions.async_insert(
        app_id=manifest["app_id"],
        version=manifest["version"],
        artifact_digest="sha256:" + "a" * 64,
        language="python",
        artifact_kind="source",
        scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id=manifest["app_id"],
        version=manifest["version"],
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="PUBLISHED",
        manifest_json=manifest,
        app_version_id=version_id,
        created_at=now,
        updated_at=now,
    )


async def test_get_permission_summary_is_deterministic(install_dal: Any) -> None:
    await _seed_published(install_dal)
    summary1, hash1 = await get_permission_summary(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1"
    )
    summary2, hash2 = await get_permission_summary(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1"
    )
    assert summary1 == summary2
    assert hash1 == hash2


async def test_get_permission_summary_unknown_version_raises_404(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await get_permission_summary(install_dal, app_id="waddles.x.y.default", version="1.0.0")
    assert exc.value.status_code == 404


async def test_install_version_globally_records_a_row(install_dal: Any) -> None:
    await _seed_published(install_dal)
    row = await install_version_globally(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1", installed_by=1
    )
    assert row.installed_by == 1
    assert row.install_source == "human"
    assert row.permission_hash.startswith("sha256:")


async def test_install_version_globally_system_actor_has_no_installed_by(install_dal: Any) -> None:
    """Core-bundle-seeder path: `installed_by=None` is a real SQL NULL, never a fake user."""
    await _seed_published(install_dal)
    row = await install_version_globally(
        install_dal,
        app_id="waddles.socials.music.default",
        version="3.0.1",
        installed_by=None,
        install_source="system:core-seeder",
    )
    assert row.installed_by is None
    assert row.install_source == "system:core-seeder"


async def test_install_version_globally_not_published_is_refused(install_dal: Any) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="0.0.1",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="COMPILING",
        manifest_json=_MANIFEST,
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(ApiError) as exc:
        await install_version_globally(
            install_dal, app_id="waddles.socials.music.default", version="0.0.1", installed_by=1
        )
    assert exc.value.code == "version_not_published"


async def test_install_version_globally_headless_hash_mismatch_fails_closed(
    install_dal: Any,
) -> None:
    await _seed_published(install_dal)
    with pytest.raises(ApiError) as exc:
        await install_version_globally(
            install_dal,
            app_id="waddles.socials.music.default",
            version="3.0.1",
            installed_by=1,
            expected_permission_hash="sha256:" + "0" * 64,
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "permission_hash_mismatch"


async def test_install_version_globally_supersedes_the_previous_current_install(
    install_dal: Any,
) -> None:
    await _seed_published(install_dal)
    first = await install_version_globally(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1", installed_by=1
    )
    newer_manifest = {**_MANIFEST, "version": "3.0.2"}
    await _seed_published(install_dal, manifest=newer_manifest)
    second = await install_version_globally(
        install_dal, app_id="waddles.socials.music.default", version="3.0.2", installed_by=1
    )
    refreshed_first = (
        await install_dal(install_dal.app_global_installs.id == first.id).select()
    ).first()
    assert refreshed_first.superseded_by == second.id


async def test_install_version_globally_sets_app_versions_approval_id(install_dal: Any) -> None:
    await _seed_published(install_dal)
    row = await install_version_globally(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1", installed_by=1
    )
    upload = (
        await install_dal(install_dal.app_version_uploads.version == "3.0.1").select()
    ).first()
    version_row = (
        await install_dal(install_dal.app_versions.id == upload.app_version_id).select()
    ).first()
    assert version_row.approval_id == row.id


async def test_install_version_globally_missing_app_version_id_is_500(install_dal: Any) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="4.0.0",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="PUBLISHED",
        manifest_json={**_MANIFEST, "version": "4.0.0"},
        app_version_id=None,
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(ApiError) as exc:
        await install_version_globally(
            install_dal, app_id="waddles.socials.music.default", version="4.0.0", installed_by=1
        )
    assert exc.value.status_code == 500
    assert exc.value.code == "missing_app_version"


async def test_install_version_globally_does_not_activate_anywhere(install_dal: Any) -> None:
    """The core split invariant: GLOBAL install never touches app_active_versions."""
    await _seed_published(install_dal)
    await install_version_globally(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1", installed_by=1
    )
    active = await install_dal(
        install_dal.app_active_versions.app_id == "waddles.socials.music.default"
    ).select()
    assert not active


async def test_uninstall_globally_unknown_app_raises_404(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await uninstall_globally(install_dal, app_id="waddles.x.y.default", revoked_by=1)
    assert exc.value.status_code == 404


async def test_uninstall_globally_marks_revoked(install_dal: Any) -> None:
    await _seed_published(install_dal)
    installed = await install_version_globally(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1", installed_by=1
    )
    await uninstall_globally(install_dal, app_id="waddles.socials.music.default", revoked_by=2)
    refreshed = (
        await install_dal(install_dal.app_global_installs.id == installed.id).select()
    ).first()
    assert refreshed.revoked_at is not None
    assert refreshed.revoked_by == 2


async def test_uninstall_globally_cascades_to_tenant_availability(install_dal: Any) -> None:
    """Cascade rule: global uninstall hides the app in every tenant that had it available."""
    await _seed_published(install_dal)
    await install_version_globally(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1", installed_by=1
    )
    await set_available(
        install_dal, tenant_id=1, app_id="waddles.socials.music.default", updated_by=1
    )
    await uninstall_globally(install_dal, app_id="waddles.socials.music.default", revoked_by=1)
    availability = (
        await install_dal(
            (install_dal.bundle_tenant_availability.tenant_id == 1)
            & (install_dal.bundle_tenant_availability.app_id == "waddles.socials.music.default")
        ).select()
    ).first()
    assert availability.available is False


async def test_list_global_installs_returns_current_rows(install_dal: Any) -> None:
    await _seed_published(install_dal)
    await install_version_globally(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1", installed_by=1
    )
    rows = await list_global_installs(install_dal)
    assert any(r.app_id == "waddles.socials.music.default" for r in rows)


def test_classify_diff_widened_when_a_new_table_is_added() -> None:
    previous = {
        "database": [{"table": "music_queue"}],
        "egress": [],
        "streams": [],
        "capabilities": [],
        "routesTo": [],
    }
    new = {
        "database": [{"table": "music_queue"}, {"table": "music_history"}],
        "egress": [],
        "streams": [],
        "capabilities": [],
        "routesTo": [],
    }
    assert classify_diff(new, previous) == "widened"


def test_classify_diff_narrowed_when_a_table_is_removed() -> None:
    previous = {
        "database": [{"table": "music_queue"}, {"table": "music_history"}],
        "egress": [],
        "streams": [],
        "capabilities": [],
        "routesTo": [],
    }
    new = {
        "database": [{"table": "music_queue"}],
        "egress": [],
        "streams": [],
        "capabilities": [],
        "routesTo": [],
    }
    assert classify_diff(new, previous) == "narrowed"


def test_classify_diff_unchanged() -> None:
    summary = {
        "database": [{"table": "music_queue"}],
        "egress": [],
        "streams": [],
        "capabilities": [],
        "routesTo": [],
    }
    assert classify_diff(summary, summary) == "unchanged"


def test_classify_diff_initial_with_no_previous() -> None:
    summary = {"database": [], "egress": [], "streams": [], "capabilities": [], "routesTo": []}
    assert classify_diff(summary, None) == "initial"


async def test_deny_version_does_not_activate(install_dal: Any) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="9.9.9",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="UPLOADED",
        manifest_json={**_MANIFEST, "version": "9.9.9"},
        created_at=now,
        updated_at=now,
    )
    await deny_version(
        install_dal, app_id="waddles.socials.music.default", version="9.9.9", reason="bad"
    )
    active = await install_dal(
        install_dal.app_active_versions.app_id == "waddles.socials.music.default"
    ).select()
    assert not active


async def test_deny_version_sets_rejected_from_a_valid_state(install_dal: Any) -> None:
    """Deny is legal from any non-terminal state (spec Sec9.1), exercised straight off UPLOADED."""
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="3.0.1",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="UPLOADED",
        manifest_json=_MANIFEST,
        created_at=now,
        updated_at=now,
    )
    await deny_version(
        install_dal,
        app_id="waddles.socials.music.default",
        version="3.0.1",
        reason="egress host not acceptable",
    )
    row = (await install_dal(install_dal.app_version_uploads.version == "3.0.1").select()).first()
    assert row.status == "REJECTED"
    assert row.reject_reason == "egress host not acceptable"


async def test_deny_version_on_a_published_row_is_refused(install_dal: Any) -> None:
    """PUBLISHED is terminal (spec Sec9.1) -- deny must route through the state machine."""
    await _seed_published(install_dal)
    with pytest.raises(ApiError) as exc:
        await deny_version(
            install_dal,
            app_id="waddles.socials.music.default",
            version="3.0.1",
            reason="late objection",
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "invalid_state_transition"
    row = (await install_dal(install_dal.app_version_uploads.version == "3.0.1").select()).first()
    assert row.status == "PUBLISHED"  # untouched -- PUBLISHED is never mutated


async def test_deny_version_unknown_raises_404(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await deny_version(
            install_dal, app_id="waddles.x.y.default", version="1.0.0", reason="nope"
        )
    assert exc.value.status_code == 404


def _parse_derived_manifest(**overrides: object) -> Any:
    manifest = {**_MANIFEST, **overrides}
    return parse_bundle_manifest_v2(
        manifest,
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )


# Coordinator fix on PR #425 (`docs/superpowers/specs/
# 2026-09-28-bundle-permissions-and-capability-gate.md` PR #419's
# `storage.kv` permission id): `kv` is no longer in `_derive_capabilities`'s
# unconditional "always" set -- it is only derived when the manifest's own
# `permissions:` list declares `storage.kv`, the same shape-derived pattern
# `http`/`db` already use.


def test_derive_capabilities_omits_kv_when_undeclared() -> None:
    manifest = _parse_derived_manifest(permissions=[])
    caps = _derive_capabilities(manifest)
    assert KV_PERMISSION_ID not in caps


def test_derive_capabilities_includes_kv_when_declared() -> None:
    manifest = _parse_derived_manifest(permissions=[KV_PERMISSION_ID])
    caps = _derive_capabilities(manifest)
    assert KV_PERMISSION_ID in caps


def test_derive_capabilities_always_includes_the_unconditional_set() -> None:
    manifest = _parse_derived_manifest(permissions=[])
    caps = _derive_capabilities(manifest)
    assert {"context", "flags", "log", "clock"} <= caps
    assert "http" in caps  # `_MANIFEST` declares `egress`
    assert "db" in caps  # `_MANIFEST` declares `data.tables`
