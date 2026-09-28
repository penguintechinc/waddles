"""Grant storage and consent flow for the Android-style permission catalog.

`docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md`
Sec3/Sec4: the 3-tier consent flow (global catalog approval -> tenant
restriction -> community activation grant) plus revocation and the
version-upgrade re-consent gate. Every write goes through `install_dal:
AsyncDB` (R52, same convention `bundle_approval_service.py` already
established) -- no second, pydal `dal` parameter is needed here either.

On any grant change this module publishes an invalidation event onto the
Valkey stream `bundle:grants:invalidate` (spec Sec4's push-invalidation
path) -- the data-plane subscriber that turns this into a `GrantSnapshot`
refresh is a separate, parallel task (Rust `svc_process`/`svc_action`),
out of this module's scope.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB
from sqlalchemy import func, select

from services import bundle_audit, valkey_admin_client
from services.bundle_approval_service import _reparse_trusted
from services.bundle_manifest_v2 import BundleManifestV2, PermissionDeclaration
from services.bundle_permission_catalog import CORE_NAMESPACE_PREFIX, is_dangerous
from services.errors import ApiError, forbidden, not_found

logger = logging.getLogger(__name__)


async def manifest_for_version(
    install_dal: AsyncDB, *, app_id: str, version: str
) -> BundleManifestV2:
    """Rebuild the structured manifest for `(app_id, version)` from its stored upload row.

    Thin, public wrapper around `bundle_approval_service._reparse_trusted`
    (already-validated manifest, not a re-validation) -- exposed here so
    `blueprints/v1/bundle_permissions.py` never reaches for another
    module's private helper directly.
    """
    rows = await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == version)
    ).select()
    upload = rows.first()
    if upload is None:
        raise not_found(f"version {version} of {app_id} not found")
    return _reparse_trusted(upload.manifest_json)


#: Every write to a grant table publishes here (spec Sec4). Consumers:
#: `svc_process`/`svc_action`'s `GrantSnapshot` push-subscriber (a
#: separate, parallel Rust task -- not implemented by this module).
GRANT_INVALIDATION_STREAM = "bundle:grants:invalidate"

_SYSTEM_APPROVAL_SOURCE = "system:core-seeder"
_HUMAN_APPROVAL_SOURCE = "human"


def _permission_snapshot_hash(permission_ids: frozenset[str]) -> str:
    """`"sha256:" + 64 hex` over the sorted permission-id set -- same shape as `permission_hash`."""
    canonical = json.dumps(sorted(permission_ids), separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def classify_permission_diff(new_ids: frozenset[str], previous_ids: frozenset[str] | None) -> str:
    """`"initial"` | `"widened"` | `"narrowed"` | `"unchanged"` -- spec Sec3.4, permission-id level.

    Mirrors `bundle_approval_service.classify_diff()`'s own flatten-diff
    shape, scoped to permission ids instead of the 8 derived capability
    names -- any add is `widened` (mixed add+remove is conservatively
    `widened` too, matching that function's own precedent), since an
    ADDED permission must block auto-upgrade exactly like a broadened one
    (Gemini condition 6, spec Sec3.4).
    """
    if previous_ids is None:
        return "initial"
    if new_ids == previous_ids:
        return "unchanged"
    added, removed = new_ids - previous_ids, previous_ids - new_ids
    if removed and not added:
        return "narrowed"
    return "widened"


def requires_reconsent(new_ids: frozenset[str], previous_ids: frozenset[str] | None) -> bool:
    """`True` unless the diff is `narrowed`/`unchanged` -- the hard, non-bypassable upgrade block.

    Gemini condition 6 (spec Sec3.4): auto-upgrade is refused the moment
    `classify_permission_diff()` reports anything other than
    `narrowed`/`unchanged` -- there is deliberately no parameter here for
    a tenant-level "auto-approve" override to plug into.
    """
    return classify_permission_diff(new_ids, previous_ids) not in ("narrowed", "unchanged")


async def _publish_invalidation(
    *,
    tenant_id: int,
    community_id: int | None,
    app_id: str,
    version: str,
    grant_version: int,
    valkey_client: Any | None = None,
) -> None:
    """`XADD bundle:grants:invalidate` -- best-effort, never blocks the caller's own write.

    A dropped/failed publish is not fatal: spec Sec4 keeps the existing
    `BUNDLE_CONFIG_POLL_SECONDS` poll as a fallback reconciliation path
    for exactly this case.
    """
    client = valkey_client if valkey_client is not None else valkey_admin_client.build_client()
    try:
        await client.xadd(
            GRANT_INVALIDATION_STREAM,
            {
                "tenant_id": str(tenant_id),
                "community_id": "" if community_id is None else str(community_id),
                "app_id": app_id,
                "version": version,
                "grant_version": str(grant_version),
            },
        )
    except Exception:  # noqa: BLE001 -- best-effort push, poll fallback covers a dropped message
        logger.warning(
            "grant invalidation publish failed, relying on poll fallback",
            extra={"app_id": app_id, "tenant_id": tenant_id, "community_id": community_id},
        )
    finally:
        if valkey_client is None:
            await client.aclose()


async def _bump_grant_version(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    community_id: int,
    app_id: str,
    version: str,
    permission_ids: frozenset[str],
    valkey_client: Any | None = None,
) -> int:
    """Append a new grant-version row, publish invalidation, return the new grant_version."""
    table = install_dal.metadata.tables["app_permission_grant_versions"]
    async with install_dal.engine.begin() as conn:
        current = (
            await conn.execute(
                select(func.max(table.c.grant_version)).where(
                    (table.c.tenant_id == tenant_id)
                    & (table.c.community_id == community_id)
                    & (table.c.app_id == app_id)
                )
            )
        ).scalar_one_or_none()
        new_version = int(current or 0) + 1
        await conn.execute(
            table.insert().values(
                tenant_id=tenant_id,
                community_id=community_id,
                app_id=app_id,
                version=version,
                grant_version=new_version,
                permission_snapshot_hash=_permission_snapshot_hash(permission_ids),
                effective_at=datetime.now(UTC),
            )
        )
    await _publish_invalidation(
        tenant_id=tenant_id,
        community_id=community_id,
        app_id=app_id,
        version=version,
        grant_version=new_version,
        valkey_client=valkey_client,
    )
    return new_version


async def _latest_grant_version_row(
    install_dal: AsyncDB, *, tenant_id: int, community_id: int, app_id: str
) -> Any:
    """The most recent `app_permission_grant_versions` row for `(tenant, community, app_id)`."""
    rows = await install_dal(
        (install_dal.app_permission_grant_versions.tenant_id == tenant_id)
        & (install_dal.app_permission_grant_versions.community_id == community_id)
        & (install_dal.app_permission_grant_versions.app_id == app_id)
    ).select(orderby=~install_dal.app_permission_grant_versions.effective_at)
    return rows.first()


# ---------------------------------------------------------------------------
# GLOBAL tier -- app_permission_requests
# ---------------------------------------------------------------------------


async def record_permission_requests(
    install_dal: AsyncDB,
    *,
    app_id: str,
    version: str,
    declarations: tuple[PermissionDeclaration, ...],
    approved_by: int | None,
    approved_permissions: frozenset[str] | None = None,
    approval_source: str = _HUMAN_APPROVAL_SOURCE,
) -> None:
    """GLOBAL tier (spec Sec3.1): write the maximal approved-permission ceiling for one version.

    Every `dangerous` permission the manifest declares must be individually
    acknowledged via `approved_permissions` -- approving without listing
    every dangerous id it requests is refused (`incomplete_dangerous_ack`,
    422), mirroring PR #415's `destructive_schema_change_requires_ack`
    posture. `approval_source="system:core-seeder"` (Sec3.6) skips this
    ack requirement -- the seeder is hard-guarded to `CORE_NAMESPACE_PREFIX`
    by its own caller, never a human review gate.
    """
    if approval_source == _SYSTEM_APPROVAL_SOURCE:
        if not app_id.startswith(CORE_NAMESPACE_PREFIX):
            raise forbidden(
                f"approval_source={_SYSTEM_APPROVAL_SOURCE!r} is reserved for "
                f"{CORE_NAMESPACE_PREFIX!r} bundles"
            )
    else:
        dangerous_ids = {d.id for d in declarations if d.risk == "dangerous"}
        acked = approved_permissions or frozenset()
        missing = dangerous_ids - acked
        if missing:
            raise ApiError(
                f"every dangerous permission must be acknowledged, missing: {sorted(missing)}",
                422,
                "incomplete_dangerous_ack",
            )

    table = install_dal.metadata.tables["app_permission_requests"]
    now = datetime.now(UTC)
    async with install_dal.engine.begin() as conn:
        await conn.execute(
            table.delete().where((table.c.app_id == app_id) & (table.c.version == version))
        )
        if declarations:
            await conn.execute(
                table.insert(),
                [
                    {
                        "app_id": app_id,
                        "version": version,
                        "permission_id": d.id,
                        "risk": d.risk,
                        "params_json": d.params,
                        "justification": d.justification,
                        "approved_by": approved_by,
                        "approval_source": approval_source,
                        "approved_at": now,
                    }
                    for d in declarations
                ],
            )
    await bundle_audit.record(
        install_dal,
        actor_id=approved_by,
        action="permissions_approved_globally",
        target_type="app_permission_requests",
        target_id=f"{app_id}@{version}",
        details={"permission_ids": sorted(d.id for d in declarations), "source": approval_source},
    )


async def seed_core_permission_requests(
    install_dal: AsyncDB, *, app_id: str, version: str, manifest: BundleManifestV2
) -> None:
    """Sec3.6: pre-grant every permission a `waddles.core.*` bundle's manifest declares.

    Called from `seed_core_bundles.py` alongside its existing
    `install_version_globally(install_source="system:core-seeder")` call
    -- `approved_by=None` (the SYSTEM-actor convention already used for
    `app_install_approvals.approval_source`, migration 0026).
    """
    await record_permission_requests(
        install_dal,
        app_id=app_id,
        version=version,
        declarations=manifest.permission_declarations,
        approved_by=None,
        approval_source=_SYSTEM_APPROVAL_SOURCE,
    )


async def get_approved_permission_ids(
    install_dal: AsyncDB, *, app_id: str, version: str
) -> frozenset[str]:
    """The GLOBAL-tier ceiling permission-id set for `(app_id, version)`."""
    rows = await install_dal(
        (install_dal.app_permission_requests.app_id == app_id)
        & (install_dal.app_permission_requests.version == version)
    ).select()
    return frozenset(r.permission_id for r in rows)


# ---------------------------------------------------------------------------
# TENANT tier -- app_tenant_permission_restrictions
# ---------------------------------------------------------------------------


async def restrict_tenant_permissions(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    app_id: str,
    version: str,
    restricted_permission_ids: frozenset[str],
    restricted_by: int | None,
) -> None:
    """TENANT tier (spec Sec3.2): set the tenant-wide exclusion list for `app_id`.

    Replaces the tenant's previous restriction set for `app_id` wholesale
    (an opt-out list, Sec3.2). Refuses a `permission_id` the global admin
    never approved (`permission_not_in_catalog_grant`, 422) -- a tenant
    can only narrow, never widen, the global ceiling.
    """
    approved = await get_approved_permission_ids(install_dal, app_id=app_id, version=version)
    unknown = restricted_permission_ids - approved
    if unknown:
        raise ApiError(
            f"cannot restrict permissions outside the approved catalog grant: {sorted(unknown)}",
            422,
            "permission_not_in_catalog_grant",
        )

    table = install_dal.metadata.tables["app_tenant_permission_restrictions"]
    now = datetime.now(UTC)
    async with install_dal.engine.begin() as conn:
        await conn.execute(
            table.delete().where((table.c.tenant_id == tenant_id) & (table.c.app_id == app_id))
        )
        if restricted_permission_ids:
            await conn.execute(
                table.insert(),
                [
                    {
                        "tenant_id": tenant_id,
                        "app_id": app_id,
                        "permission_id": pid,
                        "restricted_by": restricted_by,
                        "restricted_at": now,
                    }
                    for pid in restricted_permission_ids
                ],
            )
    await bundle_audit.record(
        install_dal,
        actor_id=restricted_by,
        action="tenant_permissions_restricted",
        target_type="app_tenant_permission_restrictions",
        target_id=app_id,
        details={"tenant_id": tenant_id, "restricted": sorted(restricted_permission_ids)},
    )


async def get_tenant_restricted_ids(
    install_dal: AsyncDB, *, tenant_id: int, app_id: str
) -> frozenset[str]:
    """The tenant's current per-`app_id` exclusion set."""
    rows = await install_dal(
        (install_dal.app_tenant_permission_restrictions.tenant_id == tenant_id)
        & (install_dal.app_tenant_permission_restrictions.app_id == app_id)
    ).select()
    return frozenset(r.permission_id for r in rows)


# ---------------------------------------------------------------------------
# COMMUNITY tier -- community_permission_grants
# ---------------------------------------------------------------------------


async def _required_permission_ids(manifest: BundleManifestV2) -> frozenset[str]:
    """Every id the manifest declares -- all "required" (no opportunistic flag yet, Sec3.3)."""
    return frozenset(d.id for d in manifest.permission_declarations)


async def grant_community_permissions(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    community_id: int,
    app_id: str,
    version: str,
    manifest: BundleManifestV2,
    granted_permission_ids: frozenset[str],
    params_by_id: dict[str, dict[str, Any]] | None,
    granted_by: int | None,
    valkey_client: Any | None = None,
) -> int:
    """COMMUNITY tier (spec Sec3.3): the community admin's actual consent -- activation prompt.

    Refuses activation (`consent_required`, 422) if any permission the
    manifest declares is missing from `granted_permission_ids`. Refuses a
    grant outside (approved minus tenant-restricted) (`permission_not_
    in_catalog_grant`, 422). On success, bumps the per-(tenant, community,
    app) grant version and publishes the push-invalidation event (Sec4).
    Returns the new `grant_version`.
    """
    approved = await get_approved_permission_ids(install_dal, app_id=app_id, version=version)
    restricted = await get_tenant_restricted_ids(install_dal, tenant_id=tenant_id, app_id=app_id)
    allowed = approved - restricted

    unknown = granted_permission_ids - allowed
    if unknown:
        raise ApiError(
            f"cannot grant permissions outside the approved/unrestricted set: {sorted(unknown)}",
            422,
            "permission_not_in_catalog_grant",
        )

    required = await _required_permission_ids(manifest)
    missing = (required & allowed) - granted_permission_ids
    if missing:
        raise ApiError(
            f"consent required for: {sorted(missing)}",
            422,
            "consent_required",
        )

    params_by_id = params_by_id or {}
    table = install_dal.metadata.tables["community_permission_grants"]
    now = datetime.now(UTC)
    async with install_dal.engine.begin() as conn:
        await conn.execute(
            table.delete().where(
                (table.c.community_id == community_id) & (table.c.app_id == app_id)
            )
        )
        if granted_permission_ids:
            await conn.execute(
                table.insert(),
                [
                    {
                        "community_id": community_id,
                        "tenant_id": tenant_id,
                        "app_id": app_id,
                        "permission_id": pid,
                        "params_json": params_by_id.get(pid, {}),
                        "granted_by": granted_by,
                        "granted_at": now,
                    }
                    for pid in granted_permission_ids
                ],
            )

    new_version = await _bump_grant_version(
        install_dal,
        tenant_id=tenant_id,
        community_id=community_id,
        app_id=app_id,
        version=version,
        permission_ids=granted_permission_ids,
        valkey_client=valkey_client,
    )
    await bundle_audit.record(
        install_dal,
        actor_id=granted_by,
        action="community_permissions_granted",
        target_type="community_permission_grants",
        target_id=f"{app_id}@{version}",
        details={
            "tenant_id": tenant_id,
            "community_id": community_id,
            "granted": sorted(granted_permission_ids),
            "grant_version": new_version,
        },
    )
    return new_version


async def get_community_granted_ids(
    install_dal: AsyncDB, *, community_id: int, app_id: str
) -> frozenset[str]:
    """The community's current, non-revoked grant set for `app_id`."""
    rows = await install_dal(
        (install_dal.community_permission_grants.community_id == community_id)
        & (install_dal.community_permission_grants.app_id == app_id)
        & (install_dal.community_permission_grants.revoked_at == None)  # noqa: E711
    ).select()
    return frozenset(r.permission_id for r in rows)


async def deactivate_permission(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    community_id: int,
    app_id: str,
    version: str,
    permission_id: str,
    deactivated_by: int | None,
    valkey_client: Any | None = None,
) -> int:
    """Sec3.7: revoke one previously-granted permission -- without deactivating the whole bundle.

    404 if the community has no active grant for `permission_id`. Bumps
    the grant version and publishes invalidation the same as a fresh
    grant -- a revocation is itself a grant-set change (Sec4). Whether a
    revoked `dangerous`+required permission should auto-deactivate the
    whole bundle (Sec3.7's last bullet) is the caller's decision -- this
    function only performs the single-permission revoke; a caller
    checking `resolve_risk(permission_id) == "dangerous"` against the
    manifest's required set can act on the result.
    """
    table = install_dal.metadata.tables["community_permission_grants"]
    now = datetime.now(UTC)
    async with install_dal.engine.begin() as conn:
        existing = (
            await conn.execute(
                select(table.c.community_id).where(
                    (table.c.community_id == community_id)
                    & (table.c.app_id == app_id)
                    & (table.c.permission_id == permission_id)
                    & (table.c.revoked_at.is_(None))
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise not_found(f"no active grant of {permission_id!r} for {app_id!r}")
        await conn.execute(
            table.update()
            .where(
                (table.c.community_id == community_id)
                & (table.c.app_id == app_id)
                & (table.c.permission_id == permission_id)
            )
            .values(revoked_by=deactivated_by, revoked_at=now)
        )

    remaining = await get_community_granted_ids(
        install_dal, community_id=community_id, app_id=app_id
    )
    new_version = await _bump_grant_version(
        install_dal,
        tenant_id=tenant_id,
        community_id=community_id,
        app_id=app_id,
        version=version,
        permission_ids=remaining,
        valkey_client=valkey_client,
    )
    await bundle_audit.record(
        install_dal,
        actor_id=deactivated_by,
        action="community_permission_revoked",
        target_type="community_permission_grants",
        target_id=f"{app_id}@{version}",
        details={
            "tenant_id": tenant_id,
            "community_id": community_id,
            "permission_id": permission_id,
            "grant_version": new_version,
        },
    )
    if is_dangerous(permission_id):
        logger.warning(
            "dangerous permission revoked -- caller should check whether the bundle's "
            "manifest requires it and deactivate the whole bundle if so (spec Sec3.7)",
            extra={
                "app_id": app_id,
                "community_id": community_id,
                "permission_id": permission_id,
            },
        )
    return new_version


# ---------------------------------------------------------------------------
# Version upgrade re-consent (spec Sec3.4)
# ---------------------------------------------------------------------------


async def check_upgrade_reconsent(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    community_id: int,
    app_id: str,
    old_version: str,
    new_version: str,
) -> bool:
    """`True` iff `new_version` may auto-apply for this community without re-consent.

    Compares the community's CURRENT granted set (pinned to `old_version`)
    against the GLOBAL-tier approved set for `new_version` -- an add or a
    broadened bound blocks auto-upgrade unconditionally (Gemini condition
    6), never bypassable by a tenant-level default.
    """
    current_granted = await get_community_granted_ids(
        install_dal, community_id=community_id, app_id=app_id
    )
    new_approved = await get_approved_permission_ids(
        install_dal, app_id=app_id, version=new_version
    )
    return not requires_reconsent(new_approved, current_granted)
