"""Shared audit-log helper for every App Bundle lifecycle tier.

Extracted from `bundle_approval_service.py::_audit_routes_to_refusal`'s own
precedent (same table) so all three tiers (global install, tenant availability,
community activation) write `audit_log` rows the identical way -- task requirement
"audit rows for every tier change" -- without duplicating the boilerplate three
times or creating an import cycle between `bundle_approval_service.py` and
`tenant_app_availability_service.py` (both depend on this leaf module, neither
depends on the other for auditing).

GRC audit finding #3 changed the contract. This helper used to be *best-effort*: it
wrapped the insert in ``except Exception: pass`` so a logging failure could never break
the caller -- which also meant a security-relevant event (a permission grant, a global
install) could vanish with no trace whatsoever. It is now **fail-loud**:

* an `audit_log` insert failure is logged at ERROR (type + value-free cause + sanitised
  traceback), counted in ``waddles.audit.write_failures``, and raised as
  :class:`services.audit_service.AuditWriteError`; nothing is swallowed;
* every event is also appended to the tamper-evident hash chain
  (:mod:`services.audit_service`) when the tenant is entitled to the Enterprise audit
  feature. The legacy `audit_log` row keeps being written in every tier (the all-tier
  basic trail the platform audit-log route reads); the chain is the Enterprise record.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB

from services.audit_events import (
    ActorKind,
    AuditCategory,
    AuditEvent,
    is_auditable_token,
    is_valid_target_type,
    lenient_details,
    validate_details,
)
from services.audit_service import (
    AuditWriteError,
    get_audit_service,
    report_write_failure,
)


def _tenant_id_for_chain(tenant_id: int | None, details: dict[str, Any] | None) -> int | None:
    """Pick the chain's tenant: the explicit argument, else an int ``tenant_id`` in ``details``.

    Most lifecycle callers already put ``tenant_id`` in ``details``; reading it here keeps
    every existing call site working unchanged. ``bool`` is excluded (it is an ``int``).
    """
    if tenant_id is not None:
        return tenant_id
    candidate = (details or {}).get("tenant_id")
    if isinstance(candidate, int) and not isinstance(candidate, bool):
        return candidate
    return None


async def record(
    install_dal: AsyncDB,
    *,
    actor_id: int | None,
    action: str,
    target_type: str,
    target_id: str,
    details: dict[str, Any] | None = None,
    tenant_id: int | None = None,
) -> None:
    """Write one lifecycle audit event; raises :class:`AuditWriteError` if it cannot be recorded.

    `actor_id=None` is a legitimate SYSTEM-actor audit row (e.g. the
    core-bundle-seeder's `install_source="system:core-seeder"` installs) --
    `audit_log.user_id` is a nullable FK, matching `app_install_approvals.
    approved_by`'s own nullable-for-SYSTEM convention (migration 0026).

    Raises:
        AuditWriteError: the legacy row or the chain record could not be written. The
            failure is already logged and counted; callers must not swallow it.
    """
    try:
        await install_dal.audit_log.async_insert(
            user_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details or {},
            created_at=datetime.now(UTC),
        )
    except Exception as exc:
        raise report_write_failure(
            category=AuditCategory.BUNDLE.value,
            action=action,
            chain_id=None,
            attempts=1,
            exc=exc,
        ) from exc

    # The chain is PII-free by construction, so an id/type that is not identifier-shaped is
    # dropped from the *chain* record (the legacy row above keeps it) and the omission is
    # itself recorded, never silent.
    chain_details = lenient_details(details)
    chain_target: str | None = target_id
    if not is_auditable_token(target_id):
        chain_target = None
        chain_details = validate_details({**chain_details, "target_id_dropped": True})
    event = AuditEvent(
        category=AuditCategory.BUNDLE,
        action=action,
        actor_kind=ActorKind.USER if actor_id is not None else ActorKind.SYSTEM,
        actor_user_id=actor_id,
        tenant_id=_tenant_id_for_chain(tenant_id, details),
        target_type=target_type if is_valid_target_type(target_type) else None,
        target_id=chain_target,
        details=chain_details,
    )
    await get_audit_service(install_dal).record(event)


__all__ = ["AuditWriteError", "record"]
