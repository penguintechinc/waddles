"""GLOBAL-tier enable/disable of a `service` bundle, plus the installed-bundles listing.

Reuses the existing three-tier lifecycle (INSTALLED -> ENABLED -> ACTIVATED); no new
table or state machine. The global state is `app_catalog.status` (`active` | `disabled`;
`deprecated`/`yanked` already exist), the same column `marketplace_lifecycle_service`
gates `make_available` on. Disabling flips the status to `disabled` and runs the existing
ENABLED/ACTIVATED cascade (`tenant_app_availability_service.unset_available`) for every
tenant that had the bundle available; the install row and its data tables are KEPT, which
is what separates disable from `uninstall_globally`. Enabling flips back to `active`;
tenants then re-enable it themselves (their prior choice was cascaded away on disable).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB

from services import bundle_audit, tenant_app_availability_service
from services.errors import ApiError, not_found

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"


@dataclass(slots=True, frozen=True)
class InstalledBundle:
    """One current, non-revoked global install joined with its catalog row."""

    app_id: str
    name: str
    kind: str
    version: str
    digest: str | None
    installed_at: Any
    install_source: str
    status: str


@dataclass(slots=True, frozen=True)
class Dependent:
    """An installed bundle whose live manifest requests a tool from the target."""

    app_id: str
    kind: str
    version: str


def short_digest(digest: str | None) -> str | None:
    """`sha256:abcdef0123...` -> `sha256:abcdef012345` (12 hex chars)."""
    if not digest:
        return None
    algo, sep, hexpart = digest.partition(":")
    return f"{algo}:{hexpart[:12]}" if sep else digest[:12]


async def _upload_manifest(install_dal: AsyncDB, app_id: str, version: str) -> dict[str, Any]:
    """Stored manifest of `app_id@version` ({} when missing)."""
    row = (
        await install_dal(
            (install_dal.app_version_uploads.app_id == app_id)
            & (install_dal.app_version_uploads.version == version)
        ).select()
    ).first()
    manifest = getattr(row, "manifest_json", None) if row is not None else None
    return manifest if isinstance(manifest, dict) else {}


async def list_installed(install_dal: AsyncDB, *, kind: str | None = None) -> list[InstalledBundle]:
    """Current non-revoked global installs (optionally one `kind`), with catalog status/kind."""
    installs = await install_dal(
        (install_dal.app_global_installs.superseded_by == None)  # noqa: E711
        & (install_dal.app_global_installs.revoked_at == None)  # noqa: E711
    ).select(orderby=install_dal.app_global_installs.app_id)
    out: list[InstalledBundle] = []
    for inst in installs:
        cat = (await install_dal(install_dal.app_catalog.app_id == inst.app_id).select()).first()
        if cat is None:
            continue
        row_kind = cat.kind or "app"
        if kind is not None and row_kind != kind:
            continue
        ver = (await install_dal(install_dal.app_versions.id == inst.version_id).select()).first()
        manifest = await _upload_manifest(install_dal, inst.app_id, inst.version)
        out.append(
            InstalledBundle(
                app_id=inst.app_id,
                name=str(manifest.get("name") or inst.app_id),
                kind=row_kind,
                version=inst.version,
                digest=getattr(ver, "artifact_digest", None) if ver is not None else None,
                installed_at=inst.installed_at,
                install_source=inst.install_source,
                status=cat.status or STATUS_ACTIVE,
            )
        )
    return out


async def find_dependents(install_dal: AsyncDB, *, app_id: str) -> list[Dependent]:
    """Installed bundles (other than `app_id`) whose live manifest requests a tool from it."""
    found: list[Dependent] = []
    for bundle in await list_installed(install_dal):
        if bundle.app_id == app_id:
            continue
        manifest = await _upload_manifest(install_dal, bundle.app_id, bundle.version)
        requested = manifest.get("requested_tools") or []
        if any(isinstance(r, dict) and r.get("target_app_id") == app_id for r in requested):
            found.append(Dependent(bundle.app_id, bundle.kind, bundle.version))
    return found


async def _require_service(install_dal: AsyncDB, app_id: str) -> Any:
    """The catalog row of a currently-installed `service` bundle, else 404 / 422."""
    inst = (
        await install_dal(
            (install_dal.app_global_installs.app_id == app_id)
            & (install_dal.app_global_installs.superseded_by == None)  # noqa: E711
            & (install_dal.app_global_installs.revoked_at == None)  # noqa: E711
        ).select()
    ).first()
    cat = (await install_dal(install_dal.app_catalog.app_id == app_id).select()).first()
    if inst is None or cat is None:
        raise not_found(f"{app_id!r} is not currently installed")
    if (cat.kind or "app") != "service":
        raise ApiError(
            f"{app_id!r} is not a service bundle; only service bundles can be disabled/enabled",
            422,
            "not_service_bundle",
        )
    return cat


async def disable_service(
    install_dal: AsyncDB, *, app_id: str, actor_id: int | None, force: bool = False
) -> tuple[bool, int]:
    """Disable a service bundle globally. Returns `(changed, tenants_hidden)`.

    409 `has_dependents` (details.dependents) when other bundles request its tools and
    `force` is false. Idempotent: an already-disabled bundle returns `(False, 0)`.
    """
    cat = await _require_service(install_dal, app_id)
    if cat.status == STATUS_DISABLED:
        return False, 0
    dependents = await find_dependents(install_dal, app_id=app_id)
    if dependents and not force:
        raise ApiError(
            f"{app_id!r} is requested by {len(dependents)} installed bundle(s); "
            "retry with force=true to disable anyway",
            409,
            "has_dependents",
            {
                "dependents": [
                    {"appId": d.app_id, "kind": d.kind, "version": d.version} for d in dependents
                ]
            },
        )
    await install_dal(install_dal.app_catalog.app_id == app_id).update(status=STATUS_DISABLED)
    rows = await install_dal(
        (install_dal.bundle_tenant_availability.app_id == app_id)
        & (install_dal.bundle_tenant_availability.available == True)  # noqa: E712
    ).select()
    hidden = 0
    for row in rows:
        await tenant_app_availability_service.unset_available(
            install_dal, tenant_id=int(row.tenant_id), app_id=app_id, updated_by=actor_id
        )
        hidden += 1
    await bundle_audit.record(
        install_dal,
        actor_id=actor_id,
        action="service_bundle_disabled",
        target_type="app_catalog",
        target_id=app_id,
        details={
            "force": force,
            "dependents": [d.app_id for d in dependents],
            "tenants_hidden": hidden,
            "at": datetime.now(UTC).isoformat(),
        },
    )
    logger.info("service bundle disabled", extra={"app_id": app_id, "tenants_hidden": hidden})
    return True, hidden


async def enable_service(install_dal: AsyncDB, *, app_id: str, actor_id: int | None) -> bool:
    """Re-enable a disabled service bundle globally. Idempotent; returns whether it changed."""
    cat = await _require_service(install_dal, app_id)
    if cat.status != STATUS_DISABLED:
        return False
    await install_dal(install_dal.app_catalog.app_id == app_id).update(status=STATUS_ACTIVE)
    await bundle_audit.record(
        install_dal,
        actor_id=actor_id,
        action="service_bundle_enabled",
        target_type="app_catalog",
        target_id=app_id,
    )
    logger.info("service bundle enabled", extra={"app_id": app_id})
    return True


async def version_by_id(install_dal: AsyncDB, version_id: int | None) -> str | None:
    """`app_versions.version` for `version_id` (None when unset or unknown)."""
    if version_id is None:
        return None
    row = (await install_dal(install_dal.app_versions.id == version_id).select()).first()
    return str(row.version) if row is not None else None


async def installed_version(install_dal: AsyncDB, app_id: str) -> str | None:
    """Version of `app_id`'s current non-revoked global install, or None."""
    row = (
        await install_dal(
            (install_dal.app_global_installs.app_id == app_id)
            & (install_dal.app_global_installs.superseded_by == None)  # noqa: E711
            & (install_dal.app_global_installs.revoked_at == None)  # noqa: E711
        ).select()
    ).first()
    return str(row.version) if row is not None else None
