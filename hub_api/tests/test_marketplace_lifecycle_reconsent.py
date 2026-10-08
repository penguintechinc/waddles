"""Integration tests: wiring `bundle_permission_service`'s reconsent gate into real activation.

Security review HIGH finding: `check_upgrade_reconsent()`/`requires_reconsent()`
had zero production callers. These tests exercise the actual, wired-up
callers (`services.marketplace_lifecycle_service.activate_bundle()`/
`make_available()`, passed a real `install_dal`) end to end against the
`community_permission_grants`/`app_permission_requests` tables, proving a
widened permission diff actually blocks re-activation and a narrowed/
identical diff proceeds.
"""

from __future__ import annotations

from typing import Any

import pytest

from services import bundle_permission_service as perm_svc
from services.bundle_manifest_v2 import PermissionDeclaration
from services.errors import ApiError
from services.marketplace_lifecycle_service import activate_bundle, make_available

_APP_ID = "waddles.bot.shoutout.reconsent-test"
_TENANT_ID = 1


def _seed_catalog_row(dal: Any, *, app_id: str = _APP_ID, version: str = "1.0.0") -> None:
    """Insert a bare `app_catalog` row -- same shape as the concurrency test's own helper."""
    dal.app_catalog.insert(
        app_id=app_id,
        name="Reconsent Test Bundle",
        manifest_version=version,
        module="bot",
        feature="waddles.bot.shoutout",
        provider="builtin",
        execution_model="native",
        is_default=False,
        compatible_with=[],
        incompatible_with=[],
        platform_compatibility={"tested_with": "release/v3.0.X"},
        status="active",
    )
    dal.commit()


def _bump_catalog_version(dal: Any, *, app_id: str = _APP_ID, version: str) -> None:
    dal(dal.app_catalog.app_id == app_id).update(manifest_version=version)
    dal.commit()


def _make_available(dal: Any, *, app_id: str = _APP_ID, tenant_id: int = _TENANT_ID) -> None:
    dal.app_tenant_availability.insert(tenant_id=tenant_id, app_id=app_id, available=True)
    dal.commit()


async def _approve(
    install_dal: Any, *, app_id: str, version: str, permission_ids: frozenset[str]
) -> None:
    await perm_svc.record_permission_requests(
        install_dal,
        app_id=app_id,
        version=version,
        declarations=tuple(
            PermissionDeclaration(id=pid, risk="normal", justification="x")
            for pid in permission_ids
        ),
        approved_by=1,
    )


def _manifest_with(permission_ids: frozenset[str]) -> Any:
    """A minimal `BundleManifestV2`-shaped stand-in.

    `grant_community_permissions()` only ever reads `.permission_declarations`
    off whatever it's handed.
    """
    from services.bundle_manifest_v2 import BundleManifestV2, Limits

    return BundleManifestV2(
        schema_version=2,
        app_id=_APP_ID,
        name="x",
        version="1.0.0",
        feature="waddles.bot.shoutout",
        module="bot",
        provider="builtin",
        language="python",
        artifact="source",
        execution_model="native",
        is_default=False,
        stages={},
        egress=(),
        data_tables=(),
        limits=Limits(timeout_ms=2000, memory_mb=64, egress_rps=10),
        permissions=(),
        routes_to=(),
        consumes=(),
        permission_declarations=tuple(
            PermissionDeclaration(id=pid, risk="normal", justification="x")
            for pid in permission_ids
        ),
    )


@pytest.fixture
async def community_id(install_dal: Any) -> int:
    cid = await install_dal.communities.async_insert(tenant_id=_TENANT_ID, name="acme")
    return int(cid)


async def test_activate_bundle_blocks_on_widened_permission_upgrade(
    bundle_install_db: Any, install_dal: Any, community_id: int
) -> None:
    """A widened re-activation is blocked.

    A community already granted a narrower permission set is blocked from
    re-activating once the catalog version widens what it would run --
    the community's existing grant is untouched and `app_activations` is
    never (re-)enabled at the wider version.
    """
    async_dal, dal = bundle_install_db, bundle_install_db.dal
    _seed_catalog_row(dal, version="1.0.0")
    _make_available(dal)

    await _approve(
        install_dal, app_id=_APP_ID, version="1.0.0", permission_ids=frozenset({"storage.kv"})
    )
    await perm_svc.grant_community_permissions(
        install_dal,
        tenant_id=_TENANT_ID,
        community_id=community_id,
        app_id=_APP_ID,
        version="1.0.0",
        manifest=_manifest_with(frozenset({"storage.kv"})),
        granted_permission_ids=frozenset({"storage.kv"}),
        params_by_id=None,
        granted_by=1,
    )

    # GLOBAL admin approves a new version that ADDS a permission (widened).
    _bump_catalog_version(dal, version="2.0.0")
    await _approve(
        install_dal,
        app_id=_APP_ID,
        version="2.0.0",
        permission_ids=frozenset({"storage.kv", "ai.generate"}),
    )

    with pytest.raises(ApiError) as exc:
        await activate_bundle(
            async_dal,
            dal,
            community_id=community_id,
            tenant_id=_TENANT_ID,
            app_id=_APP_ID,
            config=None,
            activated_by=1,
            install_dal=install_dal,
        )
    assert exc.value.status_code == 409

    # Never (re-)enabled, and the existing grant is untouched.
    activation_row = (
        dal(
            (dal.app_activations.community_id == community_id)
            & (dal.app_activations.app_id == _APP_ID)
        )
        .select()
        .first()
    )
    assert activation_row is None
    granted = await perm_svc.get_community_granted_ids(
        install_dal, community_id=community_id, app_id=_APP_ID
    )
    assert granted == frozenset({"storage.kv"})


async def test_activate_bundle_proceeds_on_narrowed_or_unchanged_upgrade(
    bundle_install_db: Any, install_dal: Any, community_id: int
) -> None:
    """A narrowed (or identical) permission diff is NOT blocked -- activation proceeds."""
    async_dal, dal = bundle_install_db, bundle_install_db.dal
    _seed_catalog_row(dal, version="1.0.0")
    _make_available(dal)

    await _approve(
        install_dal,
        app_id=_APP_ID,
        version="1.0.0",
        permission_ids=frozenset({"storage.kv", "ai.generate"}),
    )
    await perm_svc.grant_community_permissions(
        install_dal,
        tenant_id=_TENANT_ID,
        community_id=community_id,
        app_id=_APP_ID,
        version="1.0.0",
        manifest=_manifest_with(frozenset({"storage.kv", "ai.generate"})),
        granted_permission_ids=frozenset({"storage.kv", "ai.generate"}),
        params_by_id=None,
        granted_by=1,
    )

    # GLOBAL admin approves a new version that only NARROWS the set.
    _bump_catalog_version(dal, version="2.0.0")
    await _approve(
        install_dal, app_id=_APP_ID, version="2.0.0", permission_ids=frozenset({"storage.kv"})
    )

    result = await activate_bundle(
        async_dal,
        dal,
        community_id=community_id,
        tenant_id=_TENANT_ID,
        app_id=_APP_ID,
        config=None,
        activated_by=1,
        install_dal=install_dal,
    )
    assert result is not None
    activation_row = (
        dal(
            (dal.app_activations.community_id == community_id)
            & (dal.app_activations.app_id == _APP_ID)
        )
        .select()
        .first()
    )
    assert activation_row is not None
    assert bool(activation_row.enabled) is True


async def test_activate_bundle_skips_reconsent_check_without_install_dal(
    bundle_install_db: Any, community_id: int
) -> None:
    """Back-compat: omitting `install_dal` (legacy, no permission catalog) is unaffected."""
    async_dal, dal = bundle_install_db, bundle_install_db.dal
    _seed_catalog_row(dal, version="1.0.0")
    _make_available(dal)

    result = await activate_bundle(
        async_dal,
        dal,
        community_id=community_id,
        tenant_id=_TENANT_ID,
        app_id=_APP_ID,
        config=None,
        activated_by=1,
    )
    assert result is not None


async def test_make_available_blocks_when_activated_community_pending_reconsent(
    bundle_install_db: Any, install_dal: Any, community_id: int
) -> None:
    """A tenant-wide re-affirm is blocked while a community is pending re-consent.

    Re-affirming tenant availability at a widened version is blocked
    while a community that already activated it has not re-consented.
    """
    async_dal, dal = bundle_install_db, bundle_install_db.dal
    _seed_catalog_row(dal, version="1.0.0")
    _make_available(dal)

    await _approve(
        install_dal, app_id=_APP_ID, version="1.0.0", permission_ids=frozenset({"storage.kv"})
    )
    await perm_svc.grant_community_permissions(
        install_dal,
        tenant_id=_TENANT_ID,
        community_id=community_id,
        app_id=_APP_ID,
        version="1.0.0",
        manifest=_manifest_with(frozenset({"storage.kv"})),
        granted_permission_ids=frozenset({"storage.kv"}),
        params_by_id=None,
        granted_by=1,
    )
    dal.app_activations.insert(
        community_id=community_id,
        tenant_id=_TENANT_ID,
        app_id=_APP_ID,
        enabled=True,
        config={},
        activated_by=1,
    )
    dal.commit()

    _bump_catalog_version(dal, version="2.0.0")
    await _approve(
        install_dal,
        app_id=_APP_ID,
        version="2.0.0",
        permission_ids=frozenset({"storage.kv", "ai.generate"}),
    )

    with pytest.raises(ApiError) as exc:
        await make_available(
            async_dal,
            dal,
            tenant_id=_TENANT_ID,
            app_id=_APP_ID,
            config_defaults=None,
            install_dal=install_dal,
        )
    assert exc.value.status_code == 409
