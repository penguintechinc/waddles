"""Tests for the TENANT tier: `services/tenant_app_availability_service.py`.

Second of the App Bundle 3-tier split (see `services/bundle_approval_
service.py`'s own module docstring). `set_available()`/`unset_available()`
enforce the `available <= installed` superset invariant against
`app_global_installs` (GLOBAL tier); `unset_available()`'s own cascade to
COMMUNITY-tier deactivation is exercised here via `bundle_approval_
service.activate_for_community()`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.bundle_approval_service import install_version_globally
from services.errors import ApiError
from services.tenant_app_availability_service import (
    list_availability,
    set_available,
    unset_available,
)

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
    "stages": {"process": {"entry": "x:y", "consumes": []}},
}


async def _seed_installed(install_dal: Any, *, manifest: dict = _MANIFEST) -> Any:
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
    return await install_version_globally(
        install_dal, app_id=manifest["app_id"], version=manifest["version"], installed_by=1
    )


async def test_set_available_requires_a_current_global_install(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await set_available(
            install_dal, tenant_id=1, app_id="waddles.socials.music.default", updated_by=1
        )
    assert exc.value.status_code == 409


async def test_set_available_enables_it(install_dal: Any) -> None:
    await _seed_installed(install_dal)
    row = await set_available(
        install_dal, tenant_id=1, app_id="waddles.socials.music.default", updated_by=1
    )
    assert row.available is True
    assert row.tenant_id == 1


async def test_set_available_is_idempotent_upsert(install_dal: Any) -> None:
    await _seed_installed(install_dal)
    await set_available(
        install_dal, tenant_id=1, app_id="waddles.socials.music.default", updated_by=1
    )
    await set_available(
        install_dal, tenant_id=1, app_id="waddles.socials.music.default", updated_by=2
    )
    rows = await install_dal(
        (install_dal.bundle_tenant_availability.tenant_id == 1)
        & (install_dal.bundle_tenant_availability.app_id == "waddles.socials.music.default")
    ).select()
    assert len(rows) == 1
    assert rows.first().updated_by == 2


async def test_set_available_rejects_a_non_current_pinned_version(install_dal: Any) -> None:
    await _seed_installed(install_dal)
    with pytest.raises(ApiError) as exc:
        await set_available(
            install_dal,
            tenant_id=1,
            app_id="waddles.socials.music.default",
            updated_by=1,
            pinned_version_id=999999,
        )
    assert exc.value.code == "pinned_version_not_installed"


async def test_set_available_accepts_the_current_pinned_version(install_dal: Any) -> None:
    installed = await _seed_installed(install_dal)
    row = await set_available(
        install_dal,
        tenant_id=1,
        app_id="waddles.socials.music.default",
        updated_by=1,
        pinned_version_id=installed.version_id,
    )
    assert row.pinned_version_id == installed.version_id


async def test_unset_available_unknown_raises_404(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await unset_available(
            install_dal, tenant_id=1, app_id="waddles.socials.music.default", updated_by=1
        )
    assert exc.value.status_code == 404


async def test_unset_available_disables_it(install_dal: Any) -> None:
    await _seed_installed(install_dal)
    await set_available(
        install_dal, tenant_id=1, app_id="waddles.socials.music.default", updated_by=1
    )
    await unset_available(
        install_dal, tenant_id=1, app_id="waddles.socials.music.default", updated_by=1
    )
    row = (
        await install_dal(
            (install_dal.bundle_tenant_availability.tenant_id == 1)
            & (install_dal.bundle_tenant_availability.app_id == "waddles.socials.music.default")
        ).select()
    ).first()
    assert row.available is False


async def test_unset_available_cascades_deactivates_communities(install_dal: Any) -> None:
    """Task requirement: tenant hide -> deactivates in that tenant's communities."""
    from services.bundle_approval_service import activate_for_community

    await _seed_installed(install_dal)
    await set_available(
        install_dal, tenant_id=1, app_id="waddles.socials.music.default", updated_by=1
    )
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    await activate_for_community(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id="waddles.socials.music.default",
        activated_by=1,
    )
    active_before = await install_dal(
        install_dal.app_active_versions.app_id == "waddles.socials.music.default"
    ).select()
    assert active_before

    await unset_available(
        install_dal, tenant_id=1, app_id="waddles.socials.music.default", updated_by=1
    )
    active_after = await install_dal(
        install_dal.app_active_versions.app_id == "waddles.socials.music.default"
    ).select()
    assert not active_after


async def test_list_availability_returns_rows_for_tenant(install_dal: Any) -> None:
    await _seed_installed(install_dal)
    await set_available(
        install_dal, tenant_id=1, app_id="waddles.socials.music.default", updated_by=1
    )
    rows = await list_availability(install_dal, tenant_id=1)
    assert any(r.app_id == "waddles.socials.music.default" for r in rows)
