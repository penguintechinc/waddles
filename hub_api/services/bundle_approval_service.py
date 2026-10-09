"""GLOBAL install + COMMUNITY activation -- the outer two tiers of the App Bundle lifecycle split.

Coordinator ruling (2026-09-27): the single `approve_version()` this
module used to expose conflated three independent authorization tiers
into one `platform:admin`-gated call (approve a version AND activate it
for a `(tenant, community)`, in the same transaction). Split into three
tiers, narrowing global -> tenant -> community, matching the discipline
`services/marketplace_lifecycle_service.py` already established for the
older `AppManifest` pipeline (`app_catalog` -> `app_tenant_availability`
-> `app_activations`, migration 069):

  1. GLOBAL (`platform:admin`, `install_version_globally()`/
     `uninstall_globally()`, THIS module): approve a version into the
     platform catalog (`app_global_installs`, migration 0027). Vendor
     bundles require a human `platform:admin`; first-party
     `waddles.core.*` bundles use `install_source="system:core-seeder"`
     with `installed_by=None` (`hub_api/cli/seed_core_bundles.py`, same
     SYSTEM-actor convention `app_install_approvals.approval_source`,
     migration 0026, already established). NO tenant/community
     activation happens at this tier anymore.

  2. TENANT (`tenant:admin`, `services/tenant_app_availability_service.py`):
     `bundle_tenant_availability` -- whether a globally-installed app is
     enabled in one tenant's marketplace. A tenant can only enable what
     is globally installed (superset invariant, enforced there).

  3. COMMUNITY (community-admin membership, `activate_for_community()`/
     `deactivate_for_community()`, THIS module): `app_install_approvals`
     + `app_active_versions` (both pre-existing, migrations 0022-0023,
     UNCHANGED in shape) -- activating an app requires a CURRENT
     `bundle_tenant_availability` row for its tenant (superset invariant:
     `activated <= available <= installed`). `community_id` is no longer
     optional here -- every NEW activation THROUGH THIS FUNCTION is scoped
     to one real community; the old tenant-wide `TENANT_WIDE_COMMUNITY_
     SENTINEL` write path is retired from this community-admin-facing
     surface (a pre-existing tenant-wide row written by the old
     `approve_version()` is left as historical data, still readable, never
     backfilled by this migration). `activate_tenant_wide()` (added
     2026-10-02, `hub_api/cli/seed_core_bundles.py`'s SYSTEM-actor-only
     seeder path) reinstates the sentinel write for exactly that one
     caller -- the ruling above retired the HUMAN community-admin path,
     not the sentinel convention itself, which the schema (migrations
     0022-0023) and `app_source_binding_service.py` already fully retain.

     Data-plane contract verified unchanged by inspection, not schema
     change: `core/bundle_active_set/src/query.rs::read_active_set()`
     (lines ~264-292) joins `app_active_versions` to a CURRENT
     (`superseded_by IS NULL`) `app_install_approvals` row matching
     `community_id` exactly -- `activate_for_community()` below writes
     both rows with the caller's real `community_id`, same as the old
     `approve_version()` did for its own `community_id is not None`
     branch; the join's shape and columns are untouched.

R52 (coordinator ruling, inherited from the pre-split module): every
handler reads/writes through the penguin-dal `install_dal: AsyncDB`
(`services/bundle_install_dal.py`) -- `reflect()` discovers hub-api's
entire live schema, including pre-existing tables (`app_catalog`,
`communities`, `tenants`, `audit_log`) and this migration's own new ones,
so no second, pydal `dal` parameter is needed anywhere in this module.

**Scope note.** Capability derivation (`_derive_capabilities`) is based
on the manifest's declared shape (egress non-empty => `http`,
`data_tables` non-empty => `db`, an `action` stage => `relay`;
`context`/`flags`/`log`/`clock` always) -- spec Sec9.7.1's stronger
claim (cross-checked against the component's actual imports) requires
the M2 compiler to report an imports list on its artifact callback,
which is a documented follow-on once that milestone ships the field.
Per-bundle Postgres role provisioning (spec Sec11.10, a separate
follow-on) is likewise out of this milestone's scope -- approval here
records the consent record only, it does not grant DB privileges.

**`kv` (coordinator fix on PR #425, `docs/superpowers/specs/
2026-09-28-bundle-permissions-and-capability-gate.md` PR #419's
`storage.kv` permission id): no longer in the "always" set above.** `kv`
is only added when the manifest's own `permissions:` list declares
`"storage.kv"` -- the same shape-derived pattern `http`/`db` already
use (a manifest signal, not an implicit default), reversing this
module's prior "always granted" stance for `kv` specifically. The data
plane's `bundle_host_kv::authorize::authorize_kv` reads this exact
`capabilities` list (via `app_install_approvals.summary_json`,
`bundle_active_set::ActiveBundleRow::declared_capabilities`) and denies
`kv` for any bundle that omitted the permission -- undeclared means
denied.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from flask_core.bundle_attribution import license_requires_review
from penguin_dal import AsyncDB
from sqlalchemy import select
from sqlalchemy import update as sa_update

from services import (
    app_source_binding_service,
    bundle_audit,
    bundle_signing_service,
    valkey_admin_client,
)
from services.bundle_manifest_v2 import (
    BundleManifestV2,
    ConsumeRule,
    EgressRule,
    Limits,
    parse_permission_declarations,
)
from services.bundle_version_service import STATUS_PUBLISHED, STATUS_REJECTED, advance_state
from services.errors import ApiError, conflict, not_found
from services.permission_summary_service import build_permission_summary, permission_hash

logger = logging.getLogger(__name__)


def _reparse_trusted(raw: dict[str, Any]) -> BundleManifestV2:
    """Rebuild the structured manifest from a stored, already-validated `manifest_json` blob.

    Not a re-validation -- the manifest already passed
    `bundle_manifest_v2`'s gate at upload time and is immutable
    thereafter. This just reconstructs the dataclass shape for
    summary-building.
    """
    stages = raw.get("stages", {})
    consumes = tuple(
        ConsumeRule(
            platform=rule["platform"],
            source_id=rule.get("source_id"),
            event_types=tuple(rule["event_types"]),
            filters=dict(rule.get("filters") or {}),
        )
        for rule in (stages.get("process", {}).get("consumes") or [])
    )
    egress = tuple(
        EgressRule(host=e["host"], methods=tuple(e.get("methods") or ()))
        for e in raw.get("egress") or []
    )
    limits_raw = raw.get("limits") or {}
    permission_declarations = parse_permission_declarations(
        [e for e in (raw.get("permissions") or ()) if isinstance(e, dict)]
    )
    return BundleManifestV2(
        schema_version=raw["schema_version"],
        app_id=raw["app_id"],
        name=raw["name"],
        version=raw["version"],
        feature=raw["feature"],
        module=raw["module"],
        provider=raw["provider"],
        language=raw["language"],
        artifact=raw["artifact"],
        execution_model=raw.get("execution_model", "native"),
        is_default=bool(raw.get("is_default", False)),
        stages=stages,
        egress=egress,
        data_tables=tuple((raw.get("data") or {}).get("tables") or ()),
        limits=Limits(
            timeout_ms=int(limits_raw.get("timeout_ms", 2000)),
            memory_mb=int(limits_raw.get("memory_mb", 64)),
            egress_rps=int(limits_raw.get("egress_rps", 10)),
        ),
        permissions=tuple(e for e in raw.get("permissions") or () if isinstance(e, str)),
        permission_declarations=permission_declarations,
        routes_to=tuple(raw.get("routes_to") or ()),
        consumes=consumes,
        author=raw.get("author"),
        license=raw.get("license"),
        license_requires_review=(
            license_requires_review(raw["license"]) if raw.get("license") else False
        ),
        source_url=raw.get("source_url"),
        alternative_to=tuple(raw.get("alternative_to") or ()),
        homepage_url=raw.get("homepage_url"),
        notice=raw.get("notice"),
        category=raw.get("category"),
        supported_platforms=(
            tuple(raw["supported_platforms"]) if raw.get("supported_platforms") else None
        ),
    )


#: The `kv` permission's stable id in the approved permission catalog
#: (`docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md`
#: PR #419 SS1). Both **required in the manifest's own `permissions:` list**
#: to grant the capability at all, AND the exact string added to this
#: function's `capabilities` output (rather than the bare `"kv"` every
#: other entry here uses) -- `bundle_host_kv::authorize::KV_PERMISSION_ID`
#: (Rust) checks the data plane's `CapabilitySnapshot` for this literal
#: string, so hub-api and the data plane must agree on it byte-for-byte.
KV_PERMISSION_ID = "storage.kv"


def _derive_capabilities(manifest: BundleManifestV2) -> frozenset[str]:
    """The host capabilities a manifest's declared shape implies -- see this module's scope note."""
    caps = {"context", "flags", "log", "clock"}
    if manifest.egress:
        caps.add("http")
    if manifest.data_tables:
        caps.add("db")
    if "action" in manifest.stages:
        caps.add("relay")
    if KV_PERMISSION_ID in manifest.permissions:
        caps.add(KV_PERMISSION_ID)
    return frozenset(caps)


async def _audit_routes_to_refusal(
    install_dal: AsyncDB,
    *,
    actor_id: int | None,
    app_id: str,
    version: str,
    target_app_id: str,
    reason: str,
) -> None:
    """Record a routes_to refusal -- D30 requires the refusal to be auditable, not just success."""
    await bundle_audit.record(
        install_dal,
        actor_id=actor_id,
        action="routes_to_refused",
        target_type="app_install_approvals",
        target_id=f"{app_id}@{version}",
        details={"target_app_id": target_app_id, "reason": reason},
    )


async def _validate_community_tenant(
    install_dal: AsyncDB, *, community_id: int, tenant_id: int
) -> Any:
    """Refuse a `communityId` that does not exist or belongs to a different tenant.

    404 (not 403) deliberately masks whether the community exists at all
    outside the caller's tenant -- same IDOR-masking rationale as
    `services.admin_service._require_community`.

    Returns the `communities` row -- `activate_for_community()` reuses
    its `.name` as the stream-key community segment, rather than a
    second query for the same row.
    """
    rows = await install_dal(
        (install_dal.communities.id == community_id)
        & (install_dal.communities.tenant_id == tenant_id)
    ).select()
    row = rows.first()
    if row is None:
        raise not_found("community not found")
    return row


async def _tenant_slug(install_dal: AsyncDB, tenant_id: int) -> str:
    """The `tenants.slug` for `tenant_id` -- the tenant segment `source_stream_key` builds from."""
    rows = await install_dal(install_dal.tenants.id == tenant_id).select()
    row = rows.first()
    if row is None or not row.slug:
        raise ApiError(f"tenant {tenant_id} has no slug configured", 500, "tenant_slug_missing")
    return str(row.slug)


async def _validate_routes_to(
    install_dal: AsyncDB,
    *,
    routes_to: tuple[str, ...],
    tenant_id: int,
    activated_by: int | None,
    app_id: str,
    version: str,
) -> None:
    """Refuse a `routes_to` target that is missing, or unavailable in this tenant (D30, Sec5.9).

    Moved here (COMMUNITY tier) from the old `approve_version()` -- the
    GLOBAL tier no longer has a `tenant_id` to check against.
    "Available in this tenant" is now defined against `bundle_tenant_
    availability` (the TENANT tier's own table) rather than the old
    `app_install_approvals` scan: a routes_to target must be enabled for
    THIS tenant's marketplace, not merely installed somewhere globally.
    """
    for target_app_id in routes_to:
        catalog_rows = await install_dal(install_dal.app_catalog.app_id == target_app_id).select()
        if not catalog_rows:
            await _audit_routes_to_refusal(
                install_dal,
                actor_id=activated_by,
                app_id=app_id,
                version=version,
                target_app_id=target_app_id,
                reason="routes_to_target_not_found",
            )
            raise ApiError(
                f"routes_to target {target_app_id!r} does not exist in the app catalog",
                422,
                "routes_to_target_not_found",
            )
        available = await install_dal(
            (install_dal.bundle_tenant_availability.app_id == target_app_id)
            & (install_dal.bundle_tenant_availability.tenant_id == tenant_id)
            & (install_dal.bundle_tenant_availability.available == True)  # noqa: E712
        ).select()
        if not available:
            await _audit_routes_to_refusal(
                install_dal,
                actor_id=activated_by,
                app_id=app_id,
                version=version,
                target_app_id=target_app_id,
                reason="routes_to_cross_tenant",
            )
            raise ApiError(
                f"routes_to target {target_app_id!r} is not available in this tenant",
                422,
                "routes_to_cross_tenant",
            )


async def get_permission_summary(
    install_dal: AsyncDB, *, app_id: str, version: str
) -> tuple[dict[str, Any], str]:
    """The consent-screen summary and its hash for one uploaded version."""
    rows = await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == version)
    ).select()
    upload = rows.first()
    if upload is None:
        raise not_found(f"version {version} of {app_id} not found")
    manifest = _reparse_trusted(upload.manifest_json)
    summary = build_permission_summary(
        manifest,
        grant_labels=[
            {"platform": r.platform, "sourceId": r.source_id or "", "label": r.platform}
            for r in manifest.consumes
        ],
        component_capabilities=_derive_capabilities(manifest),
        min_tier="free",
        flag_key=manifest.feature,
        allow_private_hosts=False,
    )
    return summary, permission_hash(summary)


def classify_diff(new_summary: dict[str, Any], previous_summary: dict[str, Any] | None) -> str:
    """`"initial"` | `"widened"` | `"narrowed"` | `"unchanged"` -- spec Sec9.7.4."""
    if previous_summary is None:
        return "initial"

    def _flatten(summary: dict[str, Any]) -> set[str]:
        parts: set[str] = set()
        parts |= {
            f"stream:{s.get('platform')}:{s.get('sourceId')}" for s in summary.get("streams", [])
        }
        parts |= {f"egress:{e['host']}" for e in summary.get("egress", [])}
        parts |= {f"table:{t['table']}" for t in summary.get("database", [])}
        parts |= {f"cap:{c}" for c in summary.get("capabilities", [])}
        parts |= {f"route:{r}" for r in summary.get("routesTo", [])}
        return parts

    new_set, old_set = _flatten(new_summary), _flatten(previous_summary)
    if new_set == old_set:
        return "unchanged"
    added, removed = new_set - old_set, old_set - new_set
    if added and not removed:
        return "widened"
    if removed and not added:
        return "narrowed"
    return "widened"  # mixed add+remove is treated as widening -- the conservative choice


async def deny_version(install_dal: AsyncDB, *, app_id: str, version: str, reason: str) -> None:
    """Move the version to REJECTED with `reason`, through the spec Sec9.1 state machine.

    Pre-install rejection -- unrelated to any of the three lifecycle
    tiers below, unchanged by this milestone's split.
    """
    await advance_state(
        install_dal, app_id=app_id, version=version, target=STATUS_REJECTED, reject_reason=reason
    )


# ---------------------------------------------------------------------------
# GLOBAL tier -- app_global_installs (install / uninstall / list)
# ---------------------------------------------------------------------------


async def _write_global_install(
    install_dal: AsyncDB,
    *,
    app_id: str,
    version: str,
    version_id: int,
    computed_hash: str,
    summary: dict[str, Any],
    installed_by: int | None,
    install_source: str,
) -> int:
    """Insert `app_global_installs`, supersede the previous row, and set `app_versions.approval_id`.

    All in one transaction (same `engine.begin()` idiom the pre-split
    module used for its own multi-table atomicity, see
    `bundle_install_dal.raw_sql_write()`'s documented escape hatch).
    """
    installs_table = install_dal.metadata.tables["app_global_installs"]
    versions_table = install_dal.metadata.tables["app_versions"]
    now = datetime.now(UTC)

    async with install_dal.engine.begin() as conn:
        previous_id = (
            await conn.execute(
                select(installs_table.c.id).where(
                    (installs_table.c.app_id == app_id) & (installs_table.c.superseded_by.is_(None))
                )
            )
        ).scalar_one_or_none()

        insert_result = await conn.execute(
            installs_table.insert().values(
                app_id=app_id,
                version=version,
                version_id=version_id,
                permission_hash=computed_hash,
                summary_json=summary,
                install_source=install_source,
                installed_by=installed_by,
                installed_at=now,
            )
        )
        new_id = insert_result.inserted_primary_key[0]

        if previous_id is not None:
            await conn.execute(
                sa_update(installs_table)
                .where(installs_table.c.id == previous_id)
                .values(superseded_by=new_id)
            )

        await conn.execute(
            sa_update(versions_table)
            .where(versions_table.c.id == version_id)
            .values(approval_id=new_id)
        )

    return int(new_id)


async def install_version_globally(
    install_dal: AsyncDB,
    *,
    app_id: str,
    version: str,
    installed_by: int | None,
    expected_permission_hash: str | None = None,
    install_source: str = "human",
) -> Any:
    """GLOBAL tier: install `version` into the platform catalog. Does NOT activate it anywhere.

    Reachable through `blueprints/v1/bundle_approvals.py::post_approve`
    (`platform:admin`) for a human-gated vendor install, or through
    `hub_api/cli/seed_core_bundles.py` (`install_source=
    "system:core-seeder"`, `installed_by=None`) for a first-party
    `waddles.core.*` bundle at deploy time -- no vendor-scoped code path
    calls this with a SYSTEM `install_source` (guarded in the seeder
    itself, `services.vendor_bundle_authz.CORE_NAMESPACE_PREFIX`).

    A vendor's uploaded, PUBLISHED version is INACTIVE everywhere until
    this runs; even after it runs, no tenant sees it in their
    marketplace until a tenant-admin separately calls `services.
    tenant_app_availability_service.set_available()` for it (TENANT
    tier), and no community runs it until a community-admin separately
    calls `activate_for_community()` (COMMUNITY tier, below).
    """
    upload_rows = await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == version)
    ).select()
    upload = upload_rows.first()
    if upload is None:
        raise not_found(f"version {version} of {app_id} not found")
    if upload.status != STATUS_PUBLISHED:
        raise ApiError(
            f"version {version} of {app_id} is not published yet", 409, "version_not_published"
        )
    if upload.app_version_id is None:
        # Defensive only -- see the pre-split module's identical note:
        # a PUBLISHED row always has app_version_id set by publish time.
        raise ApiError(
            f"version {version} of {app_id} is PUBLISHED but has no app_versions row",
            500,
            "missing_app_version",
        )

    summary, computed_hash = await get_permission_summary(
        install_dal, app_id=app_id, version=version
    )
    if expected_permission_hash is not None and expected_permission_hash != computed_hash:
        raise ApiError(
            "the supplied permission_hash does not match the current summary",
            409,
            "permission_hash_mismatch",
        )

    new_id = await _write_global_install(
        install_dal,
        app_id=app_id,
        version=version,
        version_id=upload.app_version_id,
        computed_hash=computed_hash,
        summary=summary,
        installed_by=installed_by,
        install_source=install_source,
    )
    await bundle_audit.record(
        install_dal,
        actor_id=installed_by,
        action="app_installed_globally",
        target_type="app_global_installs",
        target_id=f"{app_id}@{version}",
        details={"install_source": install_source},
    )
    logger.info(
        "bundle install: version installed into the platform catalog",
        extra={"app_id": app_id, "version": version, "install_source": install_source},
    )
    return (await install_dal(install_dal.app_global_installs.id == new_id).select()).first()


async def uninstall_globally(install_dal: AsyncDB, *, app_id: str, revoked_by: int | None) -> Any:
    """GLOBAL tier: revoke `app_id`'s current platform-catalog install.

    Cascades DOWN (task requirement -- "global uninstall/deny -> hidden
    everywhere + deactivated in all communities"): every tenant currently
    showing `app_id` as available has it hidden, which itself cascades to
    deactivating it in every community of that tenant (`services.
    tenant_app_availability_service.unset_available()`'s own cascade).
    Each tenant's cascade step is best-effort -- one tenant's failure
    (logged, audited) never blocks hiding it for every other tenant.
    """
    from services import tenant_app_availability_service

    installs_table = install_dal.metadata.tables["app_global_installs"]
    now = datetime.now(UTC)

    async with install_dal.engine.begin() as conn:
        current_id = (
            await conn.execute(
                select(installs_table.c.id).where(
                    (installs_table.c.app_id == app_id)
                    & (installs_table.c.superseded_by.is_(None))
                    & (installs_table.c.revoked_at.is_(None))
                )
            )
        ).scalar_one_or_none()
        if current_id is None:
            raise not_found(f"{app_id!r} is not currently installed")
        await conn.execute(
            sa_update(installs_table)
            .where(installs_table.c.id == current_id)
            .values(revoked_at=now, revoked_by=revoked_by)
        )

    await bundle_audit.record(
        install_dal,
        actor_id=revoked_by,
        action="app_uninstalled_globally",
        target_type="app_global_installs",
        target_id=app_id,
    )

    availability_rows = await install_dal(
        (install_dal.bundle_tenant_availability.app_id == app_id)
        & (install_dal.bundle_tenant_availability.available == True)  # noqa: E712
    ).select()
    for row in availability_rows:
        tenant_id = int(row.tenant_id)
        try:
            await tenant_app_availability_service.unset_available(
                install_dal, tenant_id=tenant_id, app_id=app_id, updated_by=revoked_by
            )
        except Exception:  # noqa: BLE001 -- one tenant's failure must not abort the rest of the cascade
            await bundle_audit.record(
                install_dal,
                actor_id=revoked_by,
                action="app_uninstall_cascade_failed",
                target_type="app_global_installs",
                target_id=app_id,
                details={"tenant_id": tenant_id},
            )
    logger.info("bundle install: revoked globally", extra={"app_id": app_id})
    return (await install_dal(install_dal.app_global_installs.id == current_id).select()).first()


async def list_global_installs(install_dal: AsyncDB) -> list[Any]:
    """Every CURRENT (`superseded_by IS NULL`) `app_global_installs` row, installed or revoked."""
    rows = await install_dal(install_dal.app_global_installs.superseded_by == None).select(  # noqa: E711
        orderby=install_dal.app_global_installs.app_id
    )
    return list(rows)


# ---------------------------------------------------------------------------
# COMMUNITY tier -- app_install_approvals + app_active_versions (activate / deactivate / list)
# ---------------------------------------------------------------------------


async def _write_approval_and_activate(
    install_dal: AsyncDB,
    *,
    app_id: str,
    version: str,
    tenant_id: int,
    community_id: int,
    activated_by: int | None,
    computed_hash: str,
    summary: dict[str, Any],
    version_id: int,
    manifest: BundleManifestV2,
    approval_source: str = "human",
) -> tuple[int, dict[str, list[str]], dict[str, Any]]:
    """Write `app_install_approvals` + upsert `app_active_versions` + AUTO-BIND sources, in one tx.

    `community_id` is a required `int` (never `None`) -- see this
    module's own docstring on retiring the tenant-wide sentinel write
    path.

    Security-review fix: every `install_dal(...)`/`TableProxy` call
    (the ordinary query-builder path used everywhere else in this
    module) opens and auto-commits its OWN session --
    `penguin_dal.AsyncDB.commit()`'s own docstring says so explicitly
    ("commit is a no-op since AsyncQuerySet methods auto-commit"). Two
    such calls composed in sequence (write the approval, then activate)
    can never be atomic: a failure in the second call leaves a durably
    committed "approved" row with no matching activation -- exactly the
    dangling state this module claims to prevent. The one primitive this
    DAL exposes that spans multiple statements in a single transaction
    is the raw SQLAlchemy `engine.begin()` block (the same escape hatch
    `bundle_install_dal.raw_sql_write()` documents) -- used here via
    SQLAlchemy Core against the already-reflected `Table` objects
    (`install_dal.metadata.tables[...]`, the same `Table` a `TableProxy`
    wraps internally) rather than hand-written SQL strings, so column
    types (e.g. `summary_json`'s JSON/JSONB) are bound correctly by the
    dialect instead of needing a manual cast.

    `approved_by=None` + `approval_source="system:core-seeder"` is the
    SYSTEM-actor representation `hub_api/cli/seed_core_bundles.py` uses
    to activate a first-party `waddles.core.*` bundle with no human
    global-admin approval -- `approved_by` is a NULLABLE FK (migration
    0023), so this is a real NULL, never a fake `hub_users` row.
    `approval_source` (migration 0026) defaults to `"human"`, matching
    every existing caller's unchanged behavior.

    Returns `(new_id, bound, signing_result)`: the new
    `app_install_approvals.id`, `app_source_binding_service.
    sync_bindings()`'s own return value (the caller uses `bound` to
    provision consumer groups AFTER this transaction commits -- see
    `approve_version()`), and `bundle_signing_service.
    sign_and_record_version()`'s return value (the caller uses it to
    upload the signed bucket sidecar, likewise AFTER commit). All four
    writes (approval, artifact signature, AUTO-BIND, activation) commit
    together, or (on any exception before the `async with` block exits)
    none does -- verified by
    `test_bundle_approval_service.py::test_approve_version_rolls_back_the_approval_if_activation_fails`
    and its artifact-signing counterpart.
    """
    approvals_table = install_dal.metadata.tables["app_install_approvals"]
    active_table = install_dal.metadata.tables["app_active_versions"]
    versions_table = install_dal.metadata.tables["app_versions"]
    now = datetime.now(UTC)

    async with install_dal.engine.begin() as conn:
        previous_id = (
            await conn.execute(
                select(approvals_table.c.id).where(
                    (approvals_table.c.app_id == app_id)
                    & (approvals_table.c.tenant_id == tenant_id)
                    & (approvals_table.c.community_id == community_id)
                    & (approvals_table.c.superseded_by.is_(None))
                )
            )
        ).scalar_one_or_none()

        insert_result = await conn.execute(
            approvals_table.insert().values(
                tenant_id=tenant_id,
                community_id=community_id,
                app_id=app_id,
                version=version,
                permission_hash=computed_hash,
                summary_json=summary,
                approved_by=activated_by,
                approved_at=now,
                approval_source=approval_source,
            )
        )
        new_id = insert_result.inserted_primary_key[0]

        if previous_id is not None:
            await conn.execute(
                sa_update(approvals_table)
                .where(approvals_table.c.id == previous_id)
                .values(superseded_by=new_id)
            )

        # Artifact signing (spec SS5.6, Gemini review condition 9): signs
        # the digest this exact approval just recorded and writes the
        # signature columns to `app_versions` -- inside the SAME
        # transaction as the approval insert above, so a signing failure
        # (no key configured, or a data-integrity gap) rolls the approval
        # back rather than leaving it unsigned. The bucket sidecar upload
        # itself is a separate, post-commit step (see `approve_version()`).
        signing_result = await bundle_signing_service.sign_and_record_version(
            conn,
            app_versions_table=versions_table,
            version_id=version_id,
            app_id=app_id,
            version=version,
            approval_id=new_id,
        )

        # AUTO-BIND: replace app_id's ingest-source bindings for this
        # (tenant, community) inside the SAME transaction as the approval
        # + activation writes above/below -- a failure anywhere in this
        # `engine.begin()` block (including the active_table write that
        # follows) rolls the bindings back too, never leaving a
        # committed binding with no matching approval/activation.
        bound = await app_source_binding_service.sync_bindings(
            conn,
            tenant_id=tenant_id,
            community_id=community_id,
            app_id=app_id,
            manifest=manifest,
            bindings_table=install_dal.metadata.tables["app_source_bindings"],
            ingest_sources_table=install_dal.metadata.tables["ingest_sources"],
        )

        active_where = (
            (active_table.c.app_id == app_id)
            & (active_table.c.tenant_id == tenant_id)
            & (active_table.c.community_id == community_id)
        )
        existing_active = (
            await conn.execute(select(active_table.c.app_id).where(active_where))
        ).first()
        if existing_active is not None:
            await conn.execute(
                sa_update(active_table)
                .where(active_where)
                .values(version_id=version_id, activated_by=activated_by, activated_at=now)
            )
        else:
            await conn.execute(
                active_table.insert().values(
                    app_id=app_id,
                    tenant_id=tenant_id,
                    community_id=community_id,
                    version_id=version_id,
                    activated_by=activated_by,
                    activated_at=now,
                )
            )

    return int(new_id), bound, signing_result


async def _write_tenant_wide_approval_and_activate(
    install_dal: AsyncDB,
    *,
    app_id: str,
    version: str,
    tenant_id: int,
    activated_by: int | None,
    computed_hash: str,
    summary: dict[str, Any],
    version_id: int,
    manifest: BundleManifestV2,
    approval_source: str,
) -> tuple[int, dict[str, list[str]]]:
    """TENANT-WIDE sibling of `_write_approval_and_activate()` -- SYSTEM actor only.

    Deliberately a SEPARATE function rather than a `community_id: int | None`
    branch bolted onto `_write_approval_and_activate()` -- that function's own
    docstring states its `community_id` is "now a required int (never None)"
    per the 2026-09-27 community-admin-surface ruling, and this function must
    never be reachable from that human-facing surface (see
    `activate_tenant_wide()`'s own docstring for why the ruling does not
    extend here).

    Writes the schema's own, still-current tenant-wide split: `app_active_
    versions.community_id = TENANT_WIDE_COMMUNITY_SENTINEL` (0 -- that
    column is `NOT NULL`, migration 0022's own comment: "0 = tenant-wide
    sentinel; communities.id never = 0") and `app_install_approvals.
    community_id = NULL` (that column IS nullable -- no sentinel needed,
    migration 0023; `core/bundle_active_set/src/entities/app_install_
    approvals.rs`'s own doc: "NULL = tenant-wide, no sentinel needed since
    it isn't a PK column"). `core/bundle_active_set::query::
    assemble_active_set`'s read-side join deliberately matches these two
    DIFFERENT raw values for the one logical scope (the "sentinel-mismatch
    rule" its own module doc names) -- this is that convention's write
    side, not a bug.
    """
    approvals_table = install_dal.metadata.tables["app_install_approvals"]
    active_table = install_dal.metadata.tables["app_active_versions"]
    now = datetime.now(UTC)
    sentinel = app_source_binding_service.TENANT_WIDE_COMMUNITY_SENTINEL

    async with install_dal.engine.begin() as conn:
        previous_id = (
            await conn.execute(
                select(approvals_table.c.id).where(
                    (approvals_table.c.app_id == app_id)
                    & (approvals_table.c.tenant_id == tenant_id)
                    & (approvals_table.c.community_id.is_(None))
                    & (approvals_table.c.superseded_by.is_(None))
                )
            )
        ).scalar_one_or_none()

        insert_result = await conn.execute(
            approvals_table.insert().values(
                tenant_id=tenant_id,
                community_id=None,
                app_id=app_id,
                version=version,
                permission_hash=computed_hash,
                summary_json=summary,
                approved_by=activated_by,
                approved_at=now,
                approval_source=approval_source,
            )
        )
        new_id = insert_result.inserted_primary_key[0]

        if previous_id is not None:
            await conn.execute(
                sa_update(approvals_table)
                .where(approvals_table.c.id == previous_id)
                .values(superseded_by=new_id)
            )

        bound = await app_source_binding_service.sync_bindings(
            conn,
            tenant_id=tenant_id,
            community_id=None,
            app_id=app_id,
            manifest=manifest,
            bindings_table=install_dal.metadata.tables["app_source_bindings"],
            ingest_sources_table=install_dal.metadata.tables["ingest_sources"],
        )

        active_where = (
            (active_table.c.app_id == app_id)
            & (active_table.c.tenant_id == tenant_id)
            & (active_table.c.community_id == sentinel)
        )
        existing_active = (
            await conn.execute(select(active_table.c.app_id).where(active_where))
        ).first()
        if existing_active is not None:
            await conn.execute(
                sa_update(active_table)
                .where(active_where)
                .values(version_id=version_id, activated_by=activated_by, activated_at=now)
            )
        else:
            await conn.execute(
                active_table.insert().values(
                    app_id=app_id,
                    tenant_id=tenant_id,
                    community_id=sentinel,
                    version_id=version_id,
                    activated_by=activated_by,
                    activated_at=now,
                )
            )

    return int(new_id), bound


async def activate_tenant_wide(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    app_id: str,
    activated_by: int | None,
    valkey_client: Any | None = None,
    approval_source: str = "system:core-seeder",
) -> Any:
    """TENANT-WIDE activation -- SYSTEM actor only, never wired to any HTTP route.

    Reinstates the tenant-wide write path this module's own docstring
    describes as retired "for NEW writes" from `activate_for_community()` --
    that 2026-09-27 ruling narrowed the COMMUNITY-ADMIN-facing function
    specifically (closing the door on a community admin unilaterally
    activating an app platform-wide via their own membership), it did not
    remove the schema's own tenant-wide sentinel convention, which
    `app_source_binding_service.TENANT_WIDE_COMMUNITY_SENTINEL`/
    `sync_bindings()`/`provision_source_stream_groups()` already fully
    support (`community_id=None`). `hub_api/cli/seed_core_bundles.py` is
    this function's ONLY caller: `bundles/core-bundles.yaml`'s
    `activation_targets: {community_id: null}` entries need every
    `ingest_sources` row of a consumed platform bound tenant-wide (e.g.
    every Discord guild in the tenant), not one real community's --
    regression: seeder skipped activation for community_id null (alpha
    2026-10-02).

    Same 409-if-not-available / pinned-version-or-global-install
    resolution as `activate_for_community()`, minus `_validate_community_
    tenant()` (there is no community row to validate -- 404 is
    impossible by construction, not skipped).
    """
    availability_rows = await install_dal(
        (install_dal.bundle_tenant_availability.tenant_id == tenant_id)
        & (install_dal.bundle_tenant_availability.app_id == app_id)
        & (install_dal.bundle_tenant_availability.available == True)  # noqa: E712
    ).select()
    availability = availability_rows.first()
    if availability is None:
        raise conflict(f"{app_id!r} is not available in this tenant's marketplace")

    if availability.pinned_version_id is not None:
        version_id = int(availability.pinned_version_id)
        version_rows = await install_dal(install_dal.app_versions.id == version_id).select()
        version_row = version_rows.first()
        if version_row is None:  # pragma: no cover - defensive, FK guarantees this in production
            raise ApiError(f"pinned version {version_id} not found", 500, "missing_app_version")
        version = str(version_row.version)
    else:
        install_rows = await install_dal(
            (install_dal.app_global_installs.app_id == app_id)
            & (install_dal.app_global_installs.superseded_by == None)  # noqa: E711
            & (install_dal.app_global_installs.revoked_at == None)  # noqa: E711
        ).select()
        install = install_rows.first()
        if install is None:  # pragma: no cover - defensive, availability implies a current install
            raise conflict(f"{app_id!r} is not currently installed in the platform catalog")
        version_id = int(install.version_id)
        version = str(install.version)

    upload_rows = await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == version)
    ).select()
    upload = upload_rows.first()
    if upload is None:  # pragma: no cover - defensive, a global install always has a matching row
        raise not_found(f"version {version} of {app_id} not found")

    manifest = _reparse_trusted(upload.manifest_json)
    if manifest.routes_to:
        await _validate_routes_to(
            install_dal,
            routes_to=manifest.routes_to,
            tenant_id=tenant_id,
            activated_by=activated_by,
            app_id=app_id,
            version=version,
        )

    summary, computed_hash = await get_permission_summary(
        install_dal, app_id=app_id, version=version
    )

    new_id, bound = await _write_tenant_wide_approval_and_activate(
        install_dal,
        app_id=app_id,
        version=version,
        tenant_id=tenant_id,
        activated_by=activated_by,
        computed_hash=computed_hash,
        summary=summary,
        version_id=version_id,
        manifest=manifest,
        approval_source=approval_source,
    )
    await bundle_audit.record(
        install_dal,
        actor_id=activated_by,
        action="app_activated_tenant_wide",
        target_type="app_active_versions",
        target_id=f"{app_id}@{version}",
        details={"tenant_id": tenant_id, "community_id": None},
    )
    logger.info(
        "bundle activation: version activated tenant-wide",
        extra={
            "app_id": app_id,
            "version": version,
            "tenant_id": tenant_id,
            "activated_by": activated_by,
            "approval_source": approval_source,
        },
    )

    if bound:
        tenant_slug = await _tenant_slug(install_dal, tenant_id)
        client = valkey_client if valkey_client is not None else valkey_admin_client.build_client()
        try:
            await app_source_binding_service.provision_source_stream_groups(
                client,
                tenant_slug=tenant_slug,
                community_segment=None,
                app_id=app_id,
                bound=bound,
            )
        finally:
            if valkey_client is None:
                await client.aclose()

    return (await install_dal(install_dal.app_install_approvals.id == new_id).select()).first()


async def activate_for_community(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    community_id: int,
    app_id: str,
    activated_by: int | None,
    valkey_client: Any | None = None,
    approval_source: str = "human",
) -> Any:
    """COMMUNITY tier: activate `app_id` for `community_id`, AUTO-BINDing its sources.

    409 unless `app_id` has a CURRENT `bundle_tenant_availability` row for
    `tenant_id` with `available=True` (superset invariant: `activated <=
    available`). Resolves the version to activate from that availability
    row's `pinned_version_id` when set, else the platform's current
    `app_global_installs.version_id` for `app_id`.

    404s a `community_id` that does not belong to `tenant_id`, before
    anything else (IDOR-closing, same as the pre-split module).
    """
    community_row = await _validate_community_tenant(
        install_dal, community_id=community_id, tenant_id=tenant_id
    )

    availability_rows = await install_dal(
        (install_dal.bundle_tenant_availability.tenant_id == tenant_id)
        & (install_dal.bundle_tenant_availability.app_id == app_id)
        & (install_dal.bundle_tenant_availability.available == True)  # noqa: E712
    ).select()
    availability = availability_rows.first()
    if availability is None:
        raise conflict(f"{app_id!r} is not available in this tenant's marketplace")

    if availability.pinned_version_id is not None:
        version_id = int(availability.pinned_version_id)
        version_rows = await install_dal(install_dal.app_versions.id == version_id).select()
        version_row = version_rows.first()
        if version_row is None:  # pragma: no cover - defensive, FK guarantees this in production
            raise ApiError(f"pinned version {version_id} not found", 500, "missing_app_version")
        version = str(version_row.version)
    else:
        install_rows = await install_dal(
            (install_dal.app_global_installs.app_id == app_id)
            & (install_dal.app_global_installs.superseded_by == None)  # noqa: E711
            & (install_dal.app_global_installs.revoked_at == None)  # noqa: E711
        ).select()
        install = install_rows.first()
        if install is None:  # pragma: no cover - defensive, availability implies a current install
            raise conflict(f"{app_id!r} is not currently installed in the platform catalog")
        version_id = int(install.version_id)
        version = str(install.version)

    upload_rows = await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == version)
    ).select()
    upload = upload_rows.first()
    if upload is None:  # pragma: no cover - defensive, a global install always has a matching row
        raise not_found(f"version {version} of {app_id} not found")

    manifest = _reparse_trusted(upload.manifest_json)
    if manifest.routes_to:
        await _validate_routes_to(
            install_dal,
            routes_to=manifest.routes_to,
            tenant_id=tenant_id,
            activated_by=activated_by,
            app_id=app_id,
            version=version,
        )

    summary, computed_hash = await get_permission_summary(
        install_dal, app_id=app_id, version=version
    )

    new_id, bound, signing_result = await _write_approval_and_activate(
        install_dal,
        app_id=app_id,
        version=version,
        tenant_id=tenant_id,
        community_id=community_id,
        activated_by=activated_by,
        computed_hash=computed_hash,
        summary=summary,
        version_id=version_id,
        manifest=manifest,
        approval_source=approval_source,
    )
    await bundle_audit.record(
        install_dal,
        actor_id=activated_by,
        action="app_activated_for_community",
        target_type="app_active_versions",
        target_id=f"{app_id}@{version}",
        details={"tenant_id": tenant_id, "community_id": community_id},
    )
    logger.info(
        "bundle activation: version activated for community",
        extra={
            "app_id": app_id,
            "version": version,
            "tenant_id": tenant_id,
            "community_id": community_id,
            "activated_by": activated_by,
            "approval_source": approval_source,
        },
    )

    # SIGN -- AFTER the transaction above committed: uploads the signed
    # sidecar `bundle_signing_service.sign_and_record_version()` already
    # computed and recorded on `app_versions` inside that transaction.
    # Unlike the Valkey provisioning below, a failure here is NOT
    # best-effort -- it is raised straight to the caller, since an
    # executor cannot load this version at all without the bucket sidecar
    # actually carrying the signature (see that function's own doc for the
    # backfill CLI that re-runs this write idempotently).
    await bundle_signing_service.upload_signed_sidecar(**signing_result)

    # PROVISION -- AFTER the transaction above committed: ensure_group is a
    # Valkey side effect with no rollback, so it must never run inside a
    # DB transaction that might still abort (see app_source_binding_
    # service.py's own module docstring).
    if bound:
        tenant_slug = await _tenant_slug(install_dal, tenant_id)
        client = valkey_client if valkey_client is not None else valkey_admin_client.build_client()
        try:
            await app_source_binding_service.provision_source_stream_groups(
                client,
                tenant_slug=tenant_slug,
                community_segment=community_row.name,
                app_id=app_id,
                bound=bound,
            )
        finally:
            if valkey_client is None:
                await client.aclose()

    return (await install_dal(install_dal.app_install_approvals.id == new_id).select()).first()


async def deactivate_for_community(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    community_id: int,
    app_id: str,
    deactivated_by: int | None,
) -> None:
    """COMMUNITY tier: deactivate `app_id` for `community_id` -- removes the pointer AND bindings.

    404 if `app_id` is not currently active in `community_id`. Hot-unload
    contract: removing the `app_active_versions` row (rather than
    marking it disabled) means `core/bundle_active_set`'s next
    watermark-triggered read excludes it -- the same "ACTIVE, APPROVED"
    join `activate_for_community()`'s own docstring cites simply no
    longer matches this `(app_id, tenant_id, community_id)` row.
    Bindings are cleared in the SAME transaction via `app_source_
    binding_service.clear_bindings()` so a rolled-back deactivation never
    leaves a dangling unbind (or vice versa).
    """
    active_table = install_dal.metadata.tables["app_active_versions"]
    active_where = (
        (active_table.c.app_id == app_id)
        & (active_table.c.tenant_id == tenant_id)
        & (active_table.c.community_id == community_id)
    )

    async with install_dal.engine.begin() as conn:
        existing = (await conn.execute(select(active_table.c.app_id).where(active_where))).first()
        if existing is None:
            raise not_found(f"{app_id!r} is not activated for this community")
        await conn.execute(active_table.delete().where(active_where))
        await app_source_binding_service.clear_bindings(
            conn,
            tenant_id=tenant_id,
            community_id=community_id,
            app_id=app_id,
            bindings_table=install_dal.metadata.tables["app_source_bindings"],
        )

    await bundle_audit.record(
        install_dal,
        actor_id=deactivated_by,
        action="app_deactivated_for_community",
        target_type="app_active_versions",
        target_id=app_id,
        details={"tenant_id": tenant_id, "community_id": community_id},
    )
    logger.info(
        "bundle activation: deactivated for community",
        extra={"app_id": app_id, "tenant_id": tenant_id, "community_id": community_id},
    )


async def deactivate_tenant_wide(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    app_id: str,
    deactivated_by: int | None,
) -> None:
    """TENANT-WIDE sibling of `deactivate_for_community()` -- SYSTEM actor only.

    Symmetric with `activate_tenant_wide()` (requirement: "uninstall should delete them
    just like install adds them, otherwise our scale will get out of sync" --
    `core/bundle_active_set` reads this same `app_active_versions` row on every replica,
    so a stale sentinel row drifts horizontally-scaled hub-api replicas out of sync with
    each other exactly the same way a stale per-community row would).

    404 if `app_id` is not currently active tenant-wide for `tenant_id`. Matches the sentinel
    `app_active_versions.community_id = TENANT_WIDE_COMMUNITY_SENTINEL` (0) row `_write_tenant_
    wide_approval_and_activate()` wrote, rather than a real community id. `app_source_binding_
    service.sync_bindings()`'s own `active_community_id` resolution writes `app_source_bindings`
    under that SAME sentinel for a tenant-wide approval (`community_id=None` passed in, see its
    own docstring + `test_activate_tenant_wide_auto_binds_every_source_of_the_consumed_platform`'s
    assertion), so `clear_bindings()` here is called with the sentinel too -- the bindings
    table's own stored value, never the logical `None` the `app_install_approvals` table uses
    for the same scope (the sentinel-mismatch convention this module's own docstring names).

    Never wired to any HTTP route (no community-admin or tenant-admin surface can reach this --
    same restriction `activate_tenant_wide()`'s own docstring states) -- today's only caller is
    `hub_api/cli/seed_core_bundles.py`'s reconcile sweep, uninstalling a `waddles.core.*` bundle
    dropped from `bundles/core-bundles.yaml`.
    """
    active_table = install_dal.metadata.tables["app_active_versions"]
    sentinel = app_source_binding_service.TENANT_WIDE_COMMUNITY_SENTINEL
    active_where = (
        (active_table.c.app_id == app_id)
        & (active_table.c.tenant_id == tenant_id)
        & (active_table.c.community_id == sentinel)
    )

    async with install_dal.engine.begin() as conn:
        existing = (await conn.execute(select(active_table.c.app_id).where(active_where))).first()
        if existing is None:
            raise not_found(f"{app_id!r} is not activated tenant-wide for this tenant")
        await conn.execute(active_table.delete().where(active_where))
        await app_source_binding_service.clear_bindings(
            conn,
            tenant_id=tenant_id,
            community_id=sentinel,
            app_id=app_id,
            bindings_table=install_dal.metadata.tables["app_source_bindings"],
        )

    await bundle_audit.record(
        install_dal,
        actor_id=deactivated_by,
        action="app_deactivated_tenant_wide",
        target_type="app_active_versions",
        target_id=app_id,
        details={"tenant_id": tenant_id, "community_id": None},
    )
    logger.info(
        "bundle activation: deactivated tenant-wide",
        extra={"app_id": app_id, "tenant_id": tenant_id},
    )


async def list_community_activations(install_dal: AsyncDB, *, community_id: int) -> list[Any]:
    """Every `app_active_versions` row currently activated for `community_id`."""
    rows = await install_dal(install_dal.app_active_versions.community_id == community_id).select(
        orderby=install_dal.app_active_versions.app_id
    )
    return list(rows)
