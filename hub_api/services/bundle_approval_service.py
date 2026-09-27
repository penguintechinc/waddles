"""Permission-summary retrieval, approval (with widen/narrow diff), and denial (spec Sec9.7).

R52 (coordinator ruling): `app_install_approvals`/`app_version_uploads`/
`app_versions` are this milestone's own new tables, queried through the
penguin-dal `install_dal: AsyncDB` (`services/bundle_install_dal.py`) --
never a new pydal binder/query. `app_catalog`/`audit_log` are
pre-existing tables, but `install_dal.reflect()` discovers hub-api's
entire live schema at startup, not only this milestone's new tables, so
reading/writing them here through the same `install_dal` this module
already holds needs no second, pydal `dal` parameter.

**Scope note.** Capability derivation (`_derive_capabilities`) is based
on the manifest's declared shape (egress non-empty => `http`,
`data_tables` non-empty => `db`, an `action` stage => `relay`;
`context`/`kv`/`flags`/`log`/`clock` always) -- spec Sec9.7.1's stronger
claim (cross-checked against the component's actual imports) requires
the M2 compiler to report an imports list on its artifact callback,
which is a documented follow-on once that milestone ships the field.
Per-bundle Postgres role provisioning (spec Sec11.10, a separate
follow-on) is likewise out of this milestone's scope -- approval here
records the consent record only, it does not grant DB privileges.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB
from sqlalchemy import select
from sqlalchemy import update as sa_update

from services import app_source_binding_service, valkey_admin_client
from services.bundle_manifest_v2 import BundleManifestV2, ConsumeRule, EgressRule, Limits
from services.bundle_version_service import STATUS_PUBLISHED, STATUS_REJECTED, advance_state
from services.errors import ApiError, not_found
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
        permissions=tuple(raw.get("permissions") or ()),
        routes_to=tuple(raw.get("routes_to") or ()),
        consumes=consumes,
    )


def _derive_capabilities(manifest: BundleManifestV2) -> frozenset[str]:
    """The host capabilities a manifest's declared shape implies -- see this module's scope note."""
    caps = {"context", "kv", "flags", "log", "clock"}
    if manifest.egress:
        caps.add("http")
    if manifest.data_tables:
        caps.add("db")
    if "action" in manifest.stages:
        caps.add("relay")
    return frozenset(caps)


async def _audit_routes_to_refusal(
    install_dal: AsyncDB,
    *,
    actor_id: int,
    app_id: str,
    version: str,
    target_app_id: str,
    reason: str,
) -> None:
    """Record a routes_to refusal -- D30 requires the refusal to be auditable, not just success.

    Best-effort, matching this codebase's convention for every other
    audit-log write: a logging failure must never prevent the caller
    from raising the refusal itself.
    """
    try:
        await install_dal.audit_log.async_insert(
            user_id=actor_id,
            action="routes_to_refused",
            target_type="app_install_approvals",
            target_id=f"{app_id}@{version}",
            details={"target_app_id": target_app_id, "reason": reason},
            created_at=datetime.now(UTC),
        )
    except Exception:  # noqa: BLE001, S110 -- audit logging failure must not break the refusal itself
        pass


async def _validate_community_tenant(
    install_dal: AsyncDB, *, community_id: int, tenant_id: int
) -> Any:
    """Refuse a `communityId` that does not exist or belongs to a different tenant.

    404 (not 403) deliberately masks whether the community exists at all
    outside the caller's tenant -- same IDOR-masking rationale as
    `services.admin_service._require_community`. `communities` is a
    pre-existing table, reachable read-only through `install_dal` since
    `reflect()` discovers the entire live schema (see this module's own
    R52 docstring note above `_validate_routes_to`'s `app_catalog` read).

    Returns the `communities` row -- `approve_version()` reuses its
    `.name` as the stream-key community segment, rather than a second
    query for the same row.
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
    """The `tenants.slug` for `tenant_id` -- the tenant segment `source_stream_key` builds from.

    Never client-supplied -- `tenant_id` here is always the
    tenant-middleware-derived value `approve_version()` was called with.
    """
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
    approved_by: int,
    app_id: str,
    version: str,
) -> None:
    """Refuse a `routes_to` target that is missing, or in a different tenant (spec Sec5.9, D30).

    "Installed in the same tenant" is defined as: the target `app_id`
    has a non-superseded `app_install_approvals` row whose `tenant_id`
    equals the approving call's `tenant_id` -- community-agnostic, since
    a tenant-wide or any-community install of the target both count.
    `app_catalog` is a pre-existing table, reachable read-only through
    `install_dal` since `reflect()` discovers the entire live schema,
    not only this milestone's own new tables.
    """
    for target_app_id in routes_to:
        catalog_rows = await install_dal(install_dal.app_catalog.app_id == target_app_id).select()
        if not catalog_rows:
            await _audit_routes_to_refusal(
                install_dal,
                actor_id=approved_by,
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
        installed = await install_dal(
            (install_dal.app_install_approvals.app_id == target_app_id)
            & (install_dal.app_install_approvals.tenant_id == tenant_id)
            & (install_dal.app_install_approvals.superseded_by == None)  # noqa: E711 -- penguin-dal IS NULL operator
        ).select()
        if not installed:
            await _audit_routes_to_refusal(
                install_dal,
                actor_id=approved_by,
                app_id=app_id,
                version=version,
                target_app_id=target_app_id,
                reason="routes_to_cross_tenant",
            )
            raise ApiError(
                f"routes_to target {target_app_id!r} is not installed in this tenant",
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


#: `app_active_versions.community_id` sentinel for "tenant-wide" (migration
#: 0022: `communities.id` is a real SERIAL starting at 1, so it never
#: collides with 0). `approve_version()` maps a caller's `community_id=None`
#: (tenant-wide approval) to this sentinel before writing the activation
#: pointer -- `app_install_approvals.community_id` stays a nullable FK
#: (unaffected), only the separate `app_active_versions` row uses the
#: sentinel, matching that table's own NOT NULL DEFAULT 0 column.
TENANT_WIDE_COMMUNITY_SENTINEL = 0


async def _write_approval_and_activate(
    install_dal: AsyncDB,
    *,
    app_id: str,
    version: str,
    tenant_id: int,
    community_id: int | None,
    approved_by: int,
    computed_hash: str,
    summary: dict[str, Any],
    version_id: int,
    manifest: BundleManifestV2,
) -> tuple[int, dict[str, list[str]]]:
    """Write `app_install_approvals` + upsert `app_active_versions` + AUTO-BIND sources, in one tx.

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

    Returns `(new_id, bound)`: the new `app_install_approvals.id`, and
    `app_source_binding_service.sync_bindings()`'s own return value (the
    caller uses `bound` to provision consumer groups AFTER this
    transaction commits -- see `approve_version()`). All three writes
    (approval, AUTO-BIND, activation) commit together, or (on any
    exception before the `async with` block exits) none does -- verified
    by
    `test_bundle_approval_service.py::test_approve_version_rolls_back_the_approval_if_activation_fails`.
    """
    approvals_table = install_dal.metadata.tables["app_install_approvals"]
    active_table = install_dal.metadata.tables["app_active_versions"]
    active_community_id = TENANT_WIDE_COMMUNITY_SENTINEL if community_id is None else community_id
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
                approved_by=approved_by,
                approved_at=now,
            )
        )
        new_id = insert_result.inserted_primary_key[0]

        if previous_id is not None:
            await conn.execute(
                sa_update(approvals_table)
                .where(approvals_table.c.id == previous_id)
                .values(superseded_by=new_id)
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
            & (active_table.c.community_id == active_community_id)
        )
        existing_active = (
            await conn.execute(select(active_table.c.app_id).where(active_where))
        ).first()
        if existing_active is not None:
            await conn.execute(
                sa_update(active_table)
                .where(active_where)
                .values(version_id=version_id, activated_by=approved_by, activated_at=now)
            )
        else:
            await conn.execute(
                active_table.insert().values(
                    app_id=app_id,
                    tenant_id=tenant_id,
                    community_id=active_community_id,
                    version_id=version_id,
                    activated_by=approved_by,
                    activated_at=now,
                )
            )

    return int(new_id), bound


async def approve_version(
    install_dal: AsyncDB,
    *,
    app_id: str,
    version: str,
    tenant_id: int,
    community_id: int | None,
    approved_by: int,
    expected_permission_hash: str | None = None,
    valkey_client: Any | None = None,
) -> Any:
    """Record an `app_install_approvals` row, activate the version, and AUTO-BIND its sources.

    `valkey_client`, when passed (tests only), is used as-is and left
    open for the caller to manage; when `None` (the real call site) a
    fresh client is built via `valkey_admin_client.build_client()` and
    always closed here -- same convention as `bundle_version_service.
    process_prebuilt_component()`.

    Vendor separation (Justin's ruling, 2026-09-27): a vendor SUBMITS
    (`bundle_version_service.create_version`/`process_prebuilt_component`,
    reachable via `vendor:onboard`) but only a GLOBAL ADMIN may APPROVE +
    INSTALL -- this function is reachable exclusively through
    `blueprints/v1/bundle_approvals.py::post_approve`, gated on
    `@require_scope("platform:admin")`; no vendor-scoped code path calls
    it or `_write_approval_and_activate()`. A submitted version therefore stays
    absent from `app_active_versions` (INACTIVE) from upload through
    every FSM state up to and including PUBLISHED, until this function
    runs successfully.

    Refuses (404) a `community_id` that does not belong to the caller's
    tenant, before anything else -- an IDOR a client-supplied
    `communityId` would otherwise open (this module's `_validate_routes_to`
    masks the equivalent cross-tenant case for `app_install_approvals`
    the same way). Refuses (422) a `routes_to` target that does not
    exist or is installed in a different tenant, before recording
    anything (spec Sec5.9, D30) -- the runtime independently drops such
    a redirect at the stage as well; this is the install-time half.

    The `app_install_approvals` write and the `app_active_versions`
    upsert commit in a single transaction (`_write_approval_and_activate()`)
    -- both happen or neither does; a failure activating never leaves a
    durably-committed approval row with no matching activation.
    `community_id=None` (tenant-wide) maps to
    `TENANT_WIDE_COMMUNITY_SENTINEL` for the activation pointer only; the
    approval record itself keeps the nullable `community_id` as given.
    """
    community_row = None
    if community_id is not None:
        community_row = await _validate_community_tenant(
            install_dal, community_id=community_id, tenant_id=tenant_id
        )

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

    manifest = _reparse_trusted(upload.manifest_json)
    if manifest.routes_to:
        await _validate_routes_to(
            install_dal,
            routes_to=manifest.routes_to,
            tenant_id=tenant_id,
            approved_by=approved_by,
            app_id=app_id,
            version=version,
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

    if upload.app_version_id is None:
        # Defensive only -- every real PUBLISHED row's publish step (a
        # separate, not-yet-built milestone, see this module's own scope
        # note) sets `app_version_id` before advancing to PUBLISHED. A
        # PUBLISHED row with no digest pointer is a data-integrity bug,
        # never a legitimate caller state -- checked here, after every
        # other refusal (routes_to, hash mismatch) but BEFORE the approval
        # row is written, so a failure here never leaves a dangling
        # `app_install_approvals` row (or a wrongly-superseded previous
        # one) with no matching activation.
        raise ApiError(
            f"version {version} of {app_id} is PUBLISHED but has no app_versions row",
            500,
            "missing_app_version",
        )

    new_id, bound = await _write_approval_and_activate(
        install_dal,
        app_id=app_id,
        version=version,
        tenant_id=tenant_id,
        community_id=community_id,
        approved_by=approved_by,
        computed_hash=computed_hash,
        summary=summary,
        version_id=upload.app_version_id,
        manifest=manifest,
    )
    logger.info(
        "bundle approval: version activated",
        extra={
            "app_id": app_id,
            "version": version,
            "tenant_id": tenant_id,
            "community_id": community_id,
            "approved_by": approved_by,
        },
    )

    # PROVISION -- AFTER the transaction above committed: ensure_group is a
    # Valkey side effect with no rollback, so it must never run inside a
    # DB transaction that might still abort (see app_source_binding_
    # service.py's own module docstring).
    if bound:
        tenant_slug = await _tenant_slug(install_dal, tenant_id)
        community_segment = community_row.name if community_row is not None else None
        client = valkey_client if valkey_client is not None else valkey_admin_client.build_client()
        try:
            await app_source_binding_service.provision_source_stream_groups(
                client,
                tenant_slug=tenant_slug,
                community_segment=community_segment,
                app_id=app_id,
                bound=bound,
            )
        finally:
            if valkey_client is None:
                await client.aclose()

    return (await install_dal(install_dal.app_install_approvals.id == new_id).select()).first()


async def deny_version(install_dal: AsyncDB, *, app_id: str, version: str, reason: str) -> None:
    """Move the version to REJECTED with `reason`, through the spec Sec9.1 state machine.

    Routes through `advance_state()`/`valid_transition()` -- the same
    guard every other status write uses -- rather than writing
    `status="REJECTED"` directly. Denying a version already in a
    terminal state (PUBLISHED, or REJECTED again) is an illegal
    transition and raises `ApiError` 409 `invalid_state_transition`
    instead of silently mutating a row the state machine says is
    immutable (PUBLISHED is never mutated).
    """
    await advance_state(
        install_dal, app_id=app_id, version=version, target=STATUS_REJECTED, reject_reason=reason
    )
