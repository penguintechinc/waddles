"""TENANT tier: `bundle_tenant_availability` -- is a globally-installed app enabled for one tenant.

Second of the three App Bundle lifecycle tiers (see `bundle_approval_
service.py`'s own module docstring for the full split and why this table
is named `bundle_tenant_availability`, not `app_tenant_availability` --
migration 069's older, differently-shaped table for the unrelated
`AppManifest` marketplace pipeline). `set_available()` requires a CURRENT
`app_global_installs` row for `app_id` (superset invariant: a tenant can
only enable what the platform has installed); `unset_available()` cascades
DOWN to the community tier, deactivating `app_id` in every community of
this tenant that currently has it activated -- task requirement "tenant
hide -> deactivates in that tenant's communities".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB

from services import bundle_audit
from services.bundle_install_dal import raw_sql_write
from services.errors import ApiError, conflict, not_found

_AUDIT_TARGET_TYPE = "bundle_tenant_availability"


async def _current_global_install(install_dal: AsyncDB, app_id: str) -> Any:
    """The CURRENT (not superseded, not revoked) `app_global_installs` row for `app_id`."""
    rows = await install_dal(
        (install_dal.app_global_installs.app_id == app_id)
        & (install_dal.app_global_installs.superseded_by == None)  # noqa: E711
        & (install_dal.app_global_installs.revoked_at == None)  # noqa: E711
    ).select()
    return rows.first()


async def set_available(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    app_id: str,
    updated_by: int | None,
    pinned_version_id: int | None = None,
) -> Any:
    """Enable `app_id` in `tenant_id`'s marketplace. Upserts on `(tenant_id, app_id)`.

    409 unless `app_id` currently has a live `app_global_installs` row
    (superset invariant: `available <= installed`). `pinned_version_id`,
    when given, must equal that CURRENT install's own `version_id` --
    pinning to an older, since-superseded version is a documented
    follow-on (this iteration only lets a tenant acknowledge/pin
    TODAY's platform-approved version, not roll back to a historical
    one), kept deliberately narrow rather than silently accepting a
    version this tenant never actually had a chance to review.
    """
    install = await _current_global_install(install_dal, app_id)
    if install is None:
        raise conflict(f"{app_id!r} is not currently installed in the platform catalog")
    if pinned_version_id is not None and int(pinned_version_id) != int(install.version_id):
        raise ApiError(
            f"pinned_version_id {pinned_version_id} is not {app_id!r}'s currently-installed "
            "version -- pinning to a historical version is not yet supported",
            409,
            "pinned_version_not_installed",
        )

    now = datetime.now(UTC)
    existing = await install_dal(
        (install_dal.bundle_tenant_availability.tenant_id == tenant_id)
        & (install_dal.bundle_tenant_availability.app_id == app_id)
    ).select()
    if existing:
        await install_dal(
            (install_dal.bundle_tenant_availability.tenant_id == tenant_id)
            & (install_dal.bundle_tenant_availability.app_id == app_id)
        ).update(
            available=True,
            pinned_version_id=pinned_version_id,
            updated_by=updated_by,
            updated_at=now,
        )
    else:
        # `bundle_tenant_availability` has a composite PRIMARY KEY
        # (tenant_id, app_id), no surrogate id -- `TableProxy.async_insert()`
        # unconditionally reads back `inserted_primary_key[0]`, which is
        # empty for a table with no autoincrement column. Raw SQL (the
        # documented escape hatch, `bundle_install_dal.raw_sql_write()`)
        # sidesteps that assumption -- same idiom `services/bundle_
        # approval_service.py`'s own `_write_global_install()`/`_write_
        # approval_and_activate()` already use for their own composite-key
        # writes, just via a plain INSERT rather than `engine.begin()`
        # Core, since this single statement needs no cross-row atomicity.
        await raw_sql_write(
            install_dal,
            """
            INSERT INTO bundle_tenant_availability
                (tenant_id, app_id, available, pinned_version_id, updated_by, updated_at)
            VALUES (:tenant_id, :app_id, :available, :pinned_version_id, :updated_by, :updated_at)
            """,
            {
                "tenant_id": tenant_id,
                "app_id": app_id,
                "available": True,
                "pinned_version_id": pinned_version_id,
                "updated_by": updated_by,
                "updated_at": now,
            },
        )
    await bundle_audit.record(
        install_dal,
        actor_id=updated_by,
        action="tenant_availability_enabled",
        target_type=_AUDIT_TARGET_TYPE,
        target_id=f"{tenant_id}:{app_id}",
    )
    row = await install_dal(
        (install_dal.bundle_tenant_availability.tenant_id == tenant_id)
        & (install_dal.bundle_tenant_availability.app_id == app_id)
    ).select()
    return row.first()


async def unset_available(
    install_dal: AsyncDB, *, tenant_id: int, app_id: str, updated_by: int | None
) -> None:
    """Disable `app_id` for `tenant_id`. 404 if no such availability row exists.

    Cascades: deactivates `app_id` in every community of `tenant_id` that
    currently has it activated (task requirement -- a hidden app can no
    longer be running anywhere in that tenant). Each community's
    deactivation is its own already-transactional, already-audited call
    (`bundle_approval_service.deactivate_for_community`) -- imported
    locally to avoid a module-level import cycle (that module imports
    THIS one for its own global-uninstall cascade).
    """
    from services import bundle_approval_service  # noqa: PLC0415 -- see docstring, breaks the cycle

    existing = await install_dal(
        (install_dal.bundle_tenant_availability.tenant_id == tenant_id)
        & (install_dal.bundle_tenant_availability.app_id == app_id)
    ).select()
    row = existing.first()
    if row is None:
        raise not_found(f"{app_id!r} is not available for this tenant")

    now = datetime.now(UTC)
    await install_dal(
        (install_dal.bundle_tenant_availability.tenant_id == tenant_id)
        & (install_dal.bundle_tenant_availability.app_id == app_id)
    ).update(available=False, updated_by=updated_by, updated_at=now)
    await bundle_audit.record(
        install_dal,
        actor_id=updated_by,
        action="tenant_availability_disabled",
        target_type=_AUDIT_TARGET_TYPE,
        target_id=f"{tenant_id}:{app_id}",
    )

    active_rows = await install_dal(
        (install_dal.app_active_versions.tenant_id == tenant_id)
        & (install_dal.app_active_versions.app_id == app_id)
    ).select()
    for active in active_rows:
        community_id = int(active.community_id)
        try:
            await bundle_approval_service.deactivate_for_community(
                install_dal,
                tenant_id=tenant_id,
                community_id=community_id,
                app_id=app_id,
                deactivated_by=updated_by,
            )
        except Exception:  # noqa: BLE001 -- one community's failure must not abort the rest of the cascade
            await bundle_audit.record(
                install_dal,
                actor_id=updated_by,
                action="tenant_availability_cascade_deactivate_failed",
                target_type=_AUDIT_TARGET_TYPE,
                target_id=f"{tenant_id}:{app_id}",
                details={"community_id": community_id},
            )


async def list_availability(install_dal: AsyncDB, *, tenant_id: int) -> list[Any]:
    """Every `bundle_tenant_availability` row for `tenant_id` (enabled and disabled alike)."""
    rows = await install_dal(install_dal.bundle_tenant_availability.tenant_id == tenant_id).select(
        orderby=install_dal.bundle_tenant_availability.app_id
    )
    return list(rows)
