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

from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB

from services.bundle_manifest_v2 import BundleManifestV2, ConsumeRule, EgressRule, Limits
from services.bundle_version_service import STATUS_PUBLISHED
from services.errors import ApiError, not_found
from services.permission_summary_service import build_permission_summary, permission_hash


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


async def approve_version(
    install_dal: AsyncDB,
    *,
    app_id: str,
    version: str,
    tenant_id: int,
    community_id: int | None,
    approved_by: int,
    expected_permission_hash: str | None = None,
) -> Any:
    """Record an `app_install_approvals` row. Fails closed on a headless hash mismatch (Sec9.7.5).

    Refuses (422) a `routes_to` target that does not exist or is
    installed in a different tenant, before recording anything (spec
    Sec5.9, D30) -- the runtime independently drops such a redirect at
    the stage as well; this is the install-time half.
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

    previous_rows = await install_dal(
        (install_dal.app_install_approvals.app_id == app_id)
        & (install_dal.app_install_approvals.tenant_id == tenant_id)
        & (install_dal.app_install_approvals.community_id == community_id)
        & (install_dal.app_install_approvals.superseded_by == None)  # noqa: E711 -- penguin-dal IS NULL operator
    ).select()
    previous = previous_rows.first()
    now = datetime.now(UTC)
    new_id = await install_dal.app_install_approvals.async_insert(
        tenant_id=tenant_id,
        community_id=community_id,
        app_id=app_id,
        version=version,
        permission_hash=computed_hash,
        summary_json=summary,
        approved_by=approved_by,
        approved_at=now,
    )
    if previous is not None:
        await install_dal(install_dal.app_install_approvals.id == previous.id).update(
            superseded_by=new_id
        )
    return (await install_dal(install_dal.app_install_approvals.id == new_id).select()).first()


async def deny_version(install_dal: AsyncDB, *, app_id: str, version: str, reason: str) -> None:
    """Move the version to REJECTED with `reason` -- reuses `app_version_uploads.status`."""
    rows = await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == version)
    ).select()
    upload = rows.first()
    if upload is None:
        raise not_found(f"version {version} of {app_id} not found")
    await install_dal(install_dal.app_version_uploads.id == upload.id).update(
        status="REJECTED",
        reject_reason=reason,
        updated_at=datetime.now(UTC),
    )
