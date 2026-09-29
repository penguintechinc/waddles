"""Shared best-effort audit-log helper for every App Bundle lifecycle tier.

Extracted from `bundle_approval_service.py::_audit_routes_to_refusal`'s own
precedent (same table, same fire-and-forget contract) so all three tiers
(global install, tenant availability, community activation) write
`audit_log` rows the identical way -- task requirement "audit rows for
every tier change" -- without duplicating the try/except boilerplate three
times or creating an import cycle between `bundle_approval_service.py` and
`tenant_app_availability_service.py` (both depend on this leaf module,
neither depends on the other for auditing).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB

logger = logging.getLogger(__name__)


async def record(
    install_dal: AsyncDB,
    *,
    actor_id: int | None,
    action: str,
    target_type: str,
    target_id: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Best-effort `audit_log` insert -- a logging failure must never break the caller's own action.

    `actor_id=None` is a legitimate SYSTEM-actor audit row (e.g. the
    core-bundle-seeder's `install_source="system:core-seeder"` installs) --
    `audit_log.user_id` is a nullable FK, matching `app_install_approvals.
    approved_by`'s own nullable-for-SYSTEM convention (migration 0026).
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
    except Exception:  # noqa: BLE001, S110 -- audit logging failure must not break the caller's action
        pass
