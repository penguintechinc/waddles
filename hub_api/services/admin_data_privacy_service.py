"""Enterprise tenant-admin DSAR console -- access export, erasure, Do-Not-Sell, in bulk.

Statutory self-service DSAR (`data_privacy_service.py` +
`blueprints/v1/data_privacy.py`) is built for the DATA SUBJECT and stays
ungated in every tier (critical-rules.md: statutory rights are never
tier-gated). This module is the ADMIN CONVENIENCE LAYER on top: a tenant
admin runs the same three operations for a caller-SUPPLIED user id, one at
a time or in bulk. Only this layer is Enterprise (`compliance.bulk_dsar`,
gated in `blueprints/v1/admin_data_privacy.py`).

A caller-supplied user id is the textbook IDOR/BOLA surface, so every
operation here is fenced by four independent controls, in this order:

1. **Tenant membership** -- the target must belong to the ADMIN'S OWN
   tenant (the JWT `tenant` claim, never a request field): an active
   `tenant_admins` row, or a `community_members` row in a community whose
   `communities.tenant_id` is that tenant (membership uses the same
   `community_members.user_id == str(hub_users.id)` identity convention
   `community_access.py`/`admin_service.py` use). A target outside the tenant
   returns `not_found` (never "forbidden") so the console cannot be used
   as a user-existence oracle across tenants.
2. **Tenant-scoped data** -- an export's community-keyed sources are
   restricted to the admin's own tenant's communities, so a user who is
   also active in ANOTHER tenant's communities never has that tenant's
   rows disclosed here (`data_privacy_service.collect_user_data`'s
   `community_ids`).
3. **Erasure safety rails** -- `hub_users` is one global identity row, so
   erasing it is not tenant-local. An erase is refused (`conflict`) when
   the target is also a tenant admin or a non-global community member of
   ANOTHER tenant, is a platform super-admin, or is the acting admin
   themself (use self-service). Membership of the global community
   (every registered user is auto-joined, `auth_service.
   add_user_to_global_community`) is tenant-neutral and does not count.
   An erase additionally needs an explicit `confirm=True`.
4. **Mandatory audit** -- every attempt writes an `audit_log` row (actor,
   target, action, tenant, outcome) BEFORE touching any data; if that
   write fails the action is NOT performed (`AUDIT_UNAVAILABLE`). Unlike
   the best-effort audit helpers elsewhere in hub-api, an unaudited admin
   DSAR is a compliance defect, so the audit write fails closed. The row
   is updated with the final outcome afterwards (best-effort -- the
   durable "attempted" row already exists). Rows never carry PII: ids,
   counts and enum outcomes only. Denied attempts (cross-tenant, shared
   identity) are audited too.

Do-Not-Sell is stored where the CCPA/CPRA opt-out already lives:
`cookie_consent.preferences["doNotSell"]` (see `cookie_consent_service`).
The admin action is ONE-WAY (opt-out only, forces `marketing` off) -- same
rationale as the GPC header: withdrawing an opt-out is the subject's own
consent decision, never an admin's.

Uses the pydal query builder throughout (never raw `%s` SQL) -- Gotcha
#1, `hub_api/PORTING.md`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from services import data_privacy_service as privacy
from services.cookie_consent_service import (
    default_preferences,
    get_current_policy,
    log_audit_event,
)
from services.errors import ApiError, bad_request

logger = logging.getLogger(__name__)

#: Gate for this whole console -- `libs/core_platform_module/features.py`,
#: `compliance.bulk_dsar` (enterprise).
FEATURE_BULK_DSAR = "waddles.compliance.bulk_dsar"

#: Per-request caps. Exports return the subjects' data inline (chat history
#: is unbounded), so they get a tighter cap than the id-only actions.
MAX_BULK_USERS = 100
MAX_BULK_EXPORT_USERS = 25

_USER_AGENT_MAX = 512
_AUDIT_ACTION_PREFIX = "dsar."


class DsarAction(StrEnum):
    """The three admin-runnable data-subject operations."""

    EXPORT = "export"
    ERASE = "erase"
    DO_NOT_SELL = "do_not_sell"


class DsarStatus(StrEnum):
    """Per-user outcome. `completed`/`already_done` are success; the rest are not.

    `audit_unavailable` means the mandatory audit row could not be written, so
    NOTHING was done for that user.
    """

    COMPLETED = "completed"
    ALREADY_DONE = "already_done"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    FAILED = "failed"
    AUDIT_UNAVAILABLE = "audit_unavailable"


@dataclass(slots=True, frozen=True)
class DsarActor:
    """The authenticated tenant admin running the console action.

    `user_id` comes from the bearer JWT `sub`, `tenant_id`/`tenant_slug`
    from the validated `TenantContext` -- never from the request body.
    """

    user_id: int
    tenant_id: int
    tenant_slug: str
    ip_address: str | None = None
    user_agent: str | None = None


@dataclass(slots=True)
class DsarResult:
    """One target user's outcome. `data`/`incomplete` are set for a completed export only."""

    user_id: int
    status: DsarStatus
    detail: str | None = None
    data: dict[str, list[dict[str, Any]]] | None = None
    incomplete: list[dict[str, str]] | None = None


@dataclass(slots=True, frozen=True)
class TenantScope:
    """How one target user relates to the acting admin's tenant (see module docstring)."""

    exists: bool
    in_tenant: bool
    foreign_tenant_ids: frozenset[int]
    is_super_admin: bool


def max_users_for(action: DsarAction) -> int:
    """Per-request user cap for `action`."""
    return MAX_BULK_EXPORT_USERS if action is DsarAction.EXPORT else MAX_BULK_USERS


def _is_global_community(row: Any) -> bool:
    """True for the tenant-neutral global community (boolean column OR `config.is_global`)."""
    if bool(row.is_global):
        return True
    config = row.config
    return isinstance(config, dict) and config.get("is_global") in (True, "true")


async def resolve_scope(
    async_dal: Any, dal: Any, *, tenant_id: int, user_ids: Sequence[int]
) -> tuple[frozenset[int], dict[int, TenantScope]]:
    """Resolve every target's tenant relationship with a fixed number of batched queries.

    Returns `(tenant_community_ids, scopes_by_user_id)`. `user_ids` must be
    non-empty and already validated as positive ints.
    """
    tenant_community_rows = await async_dal.select_async(
        dal(dal.communities.tenant_id == tenant_id), dal.communities.id
    )
    tenant_community_ids = frozenset(int(r.id) for r in tenant_community_rows)

    user_rows = await async_dal.select_async(
        dal(dal.hub_users.id.belongs(list(user_ids))),
        dal.hub_users.id,
        dal.hub_users.is_super_admin,
    )
    users = {int(r.id): bool(r.is_super_admin) for r in user_rows}

    member_rows = await async_dal.select_async(
        dal(dal.community_members.user_id.belongs([str(u) for u in user_ids])),
        dal.community_members.user_id,
        dal.community_members.community_id,
    )
    communities_by_user: dict[int, set[int]] = {}
    for row in member_rows:
        if row.community_id is None:
            continue
        # The `belongs()` filter above only returns rows whose `user_id` is exactly
        # `str(<requested hub_users.id>)`, so a legacy non-numeric platform-identity
        # `user_id` never reaches here and `int()` cannot fail.
        communities_by_user.setdefault(int(row.user_id), set()).add(int(row.community_id))

    referenced = {cid for cids in communities_by_user.values() for cid in cids}
    community_meta: dict[int, tuple[int | None, bool]] = {}
    if referenced:
        meta_rows = await async_dal.select_async(
            dal(dal.communities.id.belongs(sorted(referenced))),
            dal.communities.id,
            dal.communities.tenant_id,
            dal.communities.is_global,
            dal.communities.config,
        )
        for row in meta_rows:
            community_meta[int(row.id)] = (
                None if row.tenant_id is None else int(row.tenant_id),
                _is_global_community(row),
            )

    admin_rows = await async_dal.select_async(
        dal(dal.tenant_admins.user_id.belongs(list(user_ids))),
        dal.tenant_admins.user_id,
        dal.tenant_admins.tenant_id,
    )
    admin_tenants: dict[int, set[int]] = {}
    for row in admin_rows:
        admin_tenants.setdefault(int(row.user_id), set()).add(int(row.tenant_id))

    scopes: dict[int, TenantScope] = {}
    for user_id in user_ids:
        exists = user_id in users
        in_tenant = user_id in admin_tenants and tenant_id in admin_tenants[user_id]
        foreign = {t for t in admin_tenants.get(user_id, set()) if t != tenant_id}
        for community_id in communities_by_user.get(user_id, set()):
            owner_tenant, is_global = community_meta.get(community_id, (None, False))
            if owner_tenant == tenant_id:
                in_tenant = True
            elif owner_tenant is not None and not is_global:
                foreign.add(owner_tenant)
        scopes[user_id] = TenantScope(
            exists=exists,
            in_tenant=exists and in_tenant,
            foreign_tenant_ids=frozenset(foreign),
            is_super_admin=users.get(user_id, False),
        )
    return tenant_community_ids, scopes


async def _audit_begin(
    async_dal: Any,
    dal: Any,
    actor: DsarActor,
    *,
    action: DsarAction,
    target_user_id: int,
    bulk_id: str | None,
) -> tuple[int, dict[str, Any]]:
    """Write the durable "attempted" audit row; raise `AUDIT_UNAVAILABLE` if it can't be written."""
    details: dict[str, Any] = {
        "tenant_id": actor.tenant_id,
        "tenant_slug": actor.tenant_slug,
        "outcome": "attempted",
        "bulk_id": bulk_id,
    }
    try:
        audit_id = await async_dal.insert_async(
            dal.audit_log,
            user_id=actor.user_id,
            action=f"{_AUDIT_ACTION_PREFIX}{action.value}",
            target_type="user",
            target_id=str(target_user_id),
            details=details,
            ip_address=actor.ip_address,
            user_agent=(actor.user_agent or "")[:_USER_AGENT_MAX] or None,
            created_at=datetime.now(UTC),
        )
    except Exception as exc:
        logger.exception(
            "dsar.audit_write_failed",
            extra={"action": action.value, "actor_id": actor.user_id, "tenant_id": actor.tenant_id},
        )
        raise ApiError(
            "Audit trail unavailable; the action was not performed", 503, "AUDIT_UNAVAILABLE"
        ) from exc
    return int(audit_id), details


async def _audit_finish(
    async_dal: Any,
    dal: Any,
    audit_id: int,
    details: dict[str, Any],
    *,
    outcome: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Record the final outcome on the already-durable audit row (best-effort)."""
    final = {**details, "outcome": outcome, **(extra or {})}
    try:
        await async_dal.update_async(dal.audit_log.id == audit_id, details=final)
    except Exception:  # noqa: BLE001 - the "attempted" row already exists; never mask the result
        logger.exception("dsar.audit_finish_failed", extra={"audit_id": audit_id})


async def _audit_denied(
    async_dal: Any,
    dal: Any,
    actor: DsarActor,
    *,
    action: DsarAction,
    target_user_id: int,
    bulk_id: str | None,
    reason: str,
) -> None:
    """Audit a refused attempt. Nothing was touched, so a write failure only logs."""
    try:
        await async_dal.insert_async(
            dal.audit_log,
            user_id=actor.user_id,
            action=f"{_AUDIT_ACTION_PREFIX}{action.value}",
            target_type="user",
            target_id=str(target_user_id),
            details={
                "tenant_id": actor.tenant_id,
                "tenant_slug": actor.tenant_slug,
                "outcome": f"denied_{reason}",
                "bulk_id": bulk_id,
            },
            ip_address=actor.ip_address,
            user_agent=(actor.user_agent or "")[:_USER_AGENT_MAX] or None,
            created_at=datetime.now(UTC),
        )
    except Exception:  # noqa: BLE001 - a denied attempt touched no data
        logger.exception("dsar.audit_denied_write_failed", extra={"reason": reason})


async def apply_do_not_sell(async_dal: Any, dal: Any, *, user_id: int) -> bool:
    """Opt `user_id` out of sale/sharing. Returns `True` if anything changed.

    Every `cookie_consent` row for the user gets `doNotSell=True` and
    `marketing=False`; a user with no consent row yet gets a new privacy-
    maximal one (`consent_method="admin_dsar"`), so the opt-out survives
    until the user's first banner interaction. Each change is mirrored into
    the subject-visible `cookie_audit_log` (best-effort, same helper the
    self-service consent flow uses). Idempotent.

    Known pre-existing limitation (documented in
    `cookie_consent_service.update_preferences`, not introduced here): a
    later self-service `PUT` of category preferences rewrites the whole
    `preferences` object without `doNotSell`.
    """
    rows = await async_dal.select_async(dal(dal.cookie_consent.user_id == user_id))
    now = datetime.now(UTC)
    changed = False
    for row in rows:
        preferences = dict(row.preferences or {})
        if preferences.get("doNotSell") is True and preferences.get("marketing") is False:
            continue
        previous_marketing = preferences.get("marketing")
        preferences["doNotSell"] = True
        preferences["marketing"] = False
        await async_dal.update_async(
            dal.cookie_consent.consent_id == row.consent_id,
            preferences=preferences,
            updated_at=now,
        )
        await log_audit_event(
            async_dal,
            dal,
            consent_id=row.consent_id,
            user_id=user_id,
            action="ADMIN_DO_NOT_SELL",
            version=row.consent_version,
            category="marketing",
            previous_value=previous_marketing if isinstance(previous_marketing, bool) else None,
            new_value=False,
        )
        changed = True

    if not rows:
        policy = await get_current_policy(async_dal, dal)
        version = policy.version if policy else "admin-dsar"
        consent_id = str(uuid4())
        await async_dal.insert_async(
            dal.cookie_consent,
            user_id=user_id,
            consent_id=consent_id,
            preferences={**default_preferences(), "doNotSell": True},
            consent_version=version,
            consent_method="admin_dsar",
            consented_at=now,
            updated_at=now,
            expires_at=None,
        )
        await log_audit_event(
            async_dal,
            dal,
            consent_id=consent_id,
            user_id=user_id,
            action="ADMIN_DO_NOT_SELL",
            version=version,
            category="marketing",
            previous_value=None,
            new_value=False,
        )
        changed = True
    return changed


def _erase_refusal(actor: DsarActor, user_id: int, scope: TenantScope) -> str | None:
    """Reason an erase must be refused for `user_id`, or `None` if it may proceed."""
    if user_id == actor.user_id:
        return "self"
    if scope.is_super_admin:
        return "super_admin"
    if scope.foreign_tenant_ids:
        return "shared_identity"
    return None


_REFUSAL_DETAIL = {
    "self": "Use the self-service data deletion for your own account",
    "super_admin": "Platform administrators cannot be erased from a tenant console",
    "shared_identity": (
        "User also belongs to another tenant; erasing the shared identity is not "
        "a tenant-local action"
    ),
}


async def _export_one(
    async_dal: Any,
    dal: Any,
    *,
    user_id: int,
    tenant_community_ids: frozenset[int],
) -> tuple[DsarResult, dict[str, Any]]:
    data, failures = await privacy.collect_user_data(
        async_dal, dal, user_id=user_id, community_ids=tenant_community_ids
    )
    result = DsarResult(
        user_id=user_id,
        status=DsarStatus.COMPLETED,
        data=data,
        incomplete=failures or None,
    )
    audit_extra: dict[str, Any] = {
        "row_counts": {source: len(rows) for source, rows in data.items()},
        "incomplete_sources": [f["source"] for f in failures],
    }
    return result, audit_extra


async def _erase_one(async_dal: Any, dal: Any, *, user_id: int) -> DsarResult:
    rows = await async_dal.select_async(dal(dal.hub_users.id == user_id), dal.hub_users.email)
    if not rows:
        return DsarResult(user_id=user_id, status=DsarStatus.NOT_FOUND, detail="User not found")
    email = rows[0].email
    if email and email.startswith(f"deleted_{user_id}@"):
        return DsarResult(user_id=user_id, status=DsarStatus.ALREADY_DONE, detail="Already erased")
    await privacy.anonymize_user_data(async_dal, dal, user_id=user_id, email=email)
    return DsarResult(user_id=user_id, status=DsarStatus.COMPLETED)


async def _process_user(
    async_dal: Any,
    dal: Any,
    *,
    actor: DsarActor,
    action: DsarAction,
    user_id: int,
    scope: TenantScope,
    tenant_community_ids: frozenset[int],
    bulk_id: str | None,
) -> DsarResult:
    """Authorize, audit, then perform `action` for one target. Never raises."""
    if not scope.in_tenant:
        await _audit_denied(
            async_dal,
            dal,
            actor,
            action=action,
            target_user_id=user_id,
            bulk_id=bulk_id,
            reason="not_in_tenant",
        )
        return DsarResult(user_id=user_id, status=DsarStatus.NOT_FOUND, detail="User not found")

    if action is DsarAction.ERASE:
        refusal = _erase_refusal(actor, user_id, scope)
        if refusal is not None:
            await _audit_denied(
                async_dal,
                dal,
                actor,
                action=action,
                target_user_id=user_id,
                bulk_id=bulk_id,
                reason=refusal,
            )
            return DsarResult(
                user_id=user_id, status=DsarStatus.CONFLICT, detail=_REFUSAL_DETAIL[refusal]
            )

    try:
        audit_id, audit_details = await _audit_begin(
            async_dal, dal, actor, action=action, target_user_id=user_id, bulk_id=bulk_id
        )
    except ApiError as exc:
        return DsarResult(user_id=user_id, status=DsarStatus.AUDIT_UNAVAILABLE, detail=exc.message)

    audit_extra: dict[str, Any] = {}
    try:
        if action is DsarAction.EXPORT:
            result, audit_extra = await _export_one(
                async_dal, dal, user_id=user_id, tenant_community_ids=tenant_community_ids
            )
        elif action is DsarAction.ERASE:
            result = await _erase_one(async_dal, dal, user_id=user_id)
        else:
            changed = await apply_do_not_sell(async_dal, dal, user_id=user_id)
            result = DsarResult(
                user_id=user_id,
                status=DsarStatus.COMPLETED if changed else DsarStatus.ALREADY_DONE,
                detail=None if changed else "Already opted out",
            )
    except Exception as exc:
        logger.exception(
            "dsar.action_failed",
            extra={"action": action.value, "actor_id": actor.user_id, "tenant_id": actor.tenant_id},
        )
        await _audit_finish(
            async_dal,
            dal,
            audit_id,
            audit_details,
            outcome="failed",
            extra={"error_type": type(exc).__name__},
        )
        return DsarResult(user_id=user_id, status=DsarStatus.FAILED, detail="Action failed")

    await _audit_finish(
        async_dal, dal, audit_id, audit_details, outcome=result.status.value, extra=audit_extra
    )
    return result


def validate_dsar_request(
    *,
    actor: DsarActor,
    action: DsarAction,
    user_ids: Sequence[int],
    confirm: bool,
) -> list[int]:
    """Validate and de-duplicate the target list; raise `ApiError` (400) on a bad request.

    Order-preserving de-dup so a repeated id never runs (and audits) twice.
    """
    if not user_ids:
        raise bad_request("userIds must be a non-empty list")
    unique: list[int] = []
    seen: set[int] = set()
    for user_id in user_ids:
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise bad_request("userIds must be positive integers")
        if user_id not in seen:
            seen.add(user_id)
            unique.append(user_id)
    cap = max_users_for(action)
    if len(unique) > cap:
        raise bad_request(f"At most {cap} users per {action.value} request")
    if action is DsarAction.ERASE and confirm is not True:
        raise bad_request("Erasure is irreversible; set confirm=true to proceed")
    logger.debug(
        "dsar.request_validated",
        extra={
            "action": action.value,
            "actor_id": actor.user_id,
            "tenant_id": actor.tenant_id,
            "targets": len(unique),
        },
    )
    return unique


async def run_dsar(
    async_dal: Any,
    dal: Any,
    *,
    actor: DsarActor,
    action: DsarAction,
    user_ids: Sequence[int],
    confirm: bool = False,
) -> list[DsarResult]:
    """Run `action` for each target, scoped to `actor`'s tenant; one result per unique target.

    Targets run sequentially (bounded by the per-action cap) and are
    isolated from each other: a failure, refusal or audit outage on one
    never aborts the rest. Raises `ApiError` (400) only for a malformed
    request, before any data is touched.
    """
    targets = validate_dsar_request(actor=actor, action=action, user_ids=user_ids, confirm=confirm)
    bulk_id = str(uuid4()) if len(targets) > 1 else None
    tenant_community_ids, scopes = await resolve_scope(
        async_dal, dal, tenant_id=actor.tenant_id, user_ids=targets
    )
    results: list[DsarResult] = []
    for user_id in targets:
        results.append(
            await _process_user(
                async_dal,
                dal,
                actor=actor,
                action=action,
                user_id=user_id,
                scope=scopes[user_id],
                tenant_community_ids=tenant_community_ids,
                bulk_id=bulk_id,
            )
        )
    return results
