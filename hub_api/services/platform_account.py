"""The bootstrap admin is a PLATFORM account, not a person: it is immutable by design.

The user seeded from `INITIAL_ADMIN_*` is marked `hub_users.is_bootstrap` (migration 0065; the
partial unique index guarantees at most one). It is the operator's break-glass identity and must
never be tied to a human's identity or PII, so it can never be deleted, deactivated, demoted from
`system` tenant admin or erased -- through any API or service path. Every refusal is audited
(actor, target uuid, action, `denied`, reason `platform_account`) and typed
(`PLATFORM_ACCOUNT_PROTECTED`). The last-admin guards still protect person accounts separately.
"""

from __future__ import annotations

import asyncio
from typing import Any

from services.audit_events import AuditCategory, AuditOutcome
from services.audit_http import record_privileged_user_change
from services.errors import ApiError

PLATFORM_ACCOUNT_CODE = "PLATFORM_ACCOUNT_PROTECTED"
PLATFORM_ACCOUNT_MESSAGE = (
    "This is the non-person platform (bootstrap) account; it cannot be deleted, deactivated, "
    "demoted or erased. Create named global admins for people instead."
)


def is_platform_account_sync(dal: Any, user_id: int) -> bool:
    """True iff `user_id` is the bootstrap platform account (`hub_users.is_bootstrap`)."""
    if "is_bootstrap" not in dal.hub_users.fields:  # reduced test bindings only
        return False
    row = dal(dal.hub_users.id == user_id).select(dal.hub_users.is_bootstrap).first()
    return bool(row is not None and row.is_bootstrap)


def platform_account_error() -> ApiError:
    """The typed 403 every refusal raises."""
    return ApiError(PLATFORM_ACCOUNT_MESSAGE, 403, PLATFORM_ACCOUNT_CODE)


async def guard_platform_account(
    async_dal: Any,
    dal: Any,
    *,
    user_id: int,
    actor_user_id: int | None,
    category: AuditCategory,
    action: str,
    operation: str,
) -> None:
    """Refuse (typed 403, audited) when `user_id` is the platform account; else return.

    `operation` is the closed verb attempted (`delete`, `deactivate`, `demote`, `erase`);
    `actor_user_id` is None for system-initiated calls.
    """
    loop = asyncio.get_running_loop()
    if not await loop.run_in_executor(async_dal.executor, is_platform_account_sync, dal, user_id):
        return
    target_uuid: str | None = None
    if "uuid" in dal.hub_users.fields:

        def _read() -> str | None:
            row = dal(dal.hub_users.id == user_id).select(dal.hub_users.uuid).first()
            return str(row.uuid) if row is not None and row.uuid else None

        target_uuid = await loop.run_in_executor(async_dal.executor, _read)
    await record_privileged_user_change(
        actor_user_id=actor_user_id,
        target_user_uuid=target_uuid,
        category=category,
        action=action,
        outcome=AuditOutcome.DENIED,
        operation=operation,
        reason="platform_account",
    )
    raise platform_account_error()


__all__ = [
    "PLATFORM_ACCOUNT_CODE",
    "guard_platform_account",
    "is_platform_account_sync",
    "platform_account_error",
]
