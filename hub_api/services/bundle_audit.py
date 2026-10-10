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

import logging
from dataclasses import dataclass
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

logger = logging.getLogger(__name__)


def _legacy_created_at(install_dal: AsyncDB) -> datetime:
    """`now()` in the form the legacy ``audit_log.created_at`` column accepts.

    The baseline column is a plain ``TIMESTAMP`` (no time zone). asyncpg refuses a tz-aware value
    for it (``can't subtract offset-naive and offset-aware datetimes``), so every legacy insert
    on real Postgres failed -- and the old ``except: pass`` hid that for as long as this helper
    existed, leaving the bundle-lifecycle trail silently empty in production. Pass UTC in
    whichever form the reflected column wants.
    """
    now = datetime.now(UTC)
    table = install_dal.metadata.tables.get("audit_log")
    column = table.c.get("created_at") if table is not None else None
    if column is not None and getattr(column.type, "timezone", False):
        return now
    return now.replace(tzinfo=None)


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
            created_at=_legacy_created_at(install_dal),
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


async def try_record(install_dal: AsyncDB, **kwargs: Any) -> AuditWriteError | None:
    """Like :func:`record` but RETURNS the :class:`AuditWriteError` instead of raising it.

    For call sites with required follow-on work (a cascade, a Valkey invalidation, a signed
    sidecar upload) that must still run even when the audit write fails, so a failed audit cannot
    leave the platform half-changed. The failure is already logged at ERROR and counted when this
    returns; the caller must surface it afterwards (use :class:`DeferredAudit`).
    """
    try:
        await record(install_dal, **kwargs)
    except AuditWriteError as exc:
        # Not swallowed: handed back so the caller raises it after its follow-on work. The
        # failure itself was already logged at ERROR and counted when the write failed.
        logger.warning("bundle audit: write failed; returning the error for the caller to raise")
        return exc
    return None


@dataclass(slots=True)
class DeferredAudit:
    """Run a flow's audit writes without letting a failure skip the rest of the flow.

    ``await deferred.record(...)`` writes (loudly logging any failure) and remembers the first
    error; call :meth:`raise_if_failed` once the follow-on work is done. Nothing is swallowed --
    the error is raised, just after the work that must not be skipped.
    """

    error: AuditWriteError | None = None

    async def record(self, install_dal: AsyncDB, **kwargs: Any) -> None:
        """Write one audit event now; keep the first failure for :meth:`raise_if_failed`."""
        failure = await try_record(install_dal, **kwargs)
        if self.error is None:
            self.error = failure

    def raise_if_failed(self) -> None:
        """Raise the first remembered :class:`AuditWriteError`, if any."""
        if self.error is not None:
            raise self.error


__all__ = ["AuditWriteError", "DeferredAudit", "record", "try_record"]
