"""Audit trail for account linking: one `audit_log` row per identity link / unlink.

Follows `sso_service._audit`: best-effort (a failed write is logged with its type, never
swallowed silently, and never aborts the user's action). PII-free by construction: the row
names the hub user by `hub_users.uuid` and carries only the platform, the action and who did
it -- never an email, username, platform handle or platform user id.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from opentelemetry import metrics

from services import schema

logger = logging.getLogger(__name__)
_meter = metrics.get_meter("hub_api.identity_audit")
_events = _meter.create_counter(
    "hub_api.identity.link_audit_events",
    description="Identity link/unlink audit rows by action and outcome (no PII labels)",
)

IdentityAction = Literal["identity.linked", "identity.unlinked"]
#: Which surface performed the change. Only the user themselves can link/unlink today.
Actor = Literal["self"]


async def record_identity_event(
    async_dal: Any,
    dal: Any,
    *,
    user_id: int,
    platform: str,
    action: IdentityAction,
    via: str,
    actor: Actor = "self",
) -> None:
    """Write one PII-free `audit_log` row for a link/unlink of ``platform`` on ``user_id``.

    ``via`` names the endpoint family (``oauth`` / ``user_identities``). ``user_id`` is the
    acting user (the internal column the other audit writers use); the target is the user's
    public UUID.
    """
    try:
        # Idempotent: `audit_log` is bound by the platform/admin groups, not by
        # `bind_auth_tables`, so never assume whichever group ran first bound it.
        schema._define_audit_log(dal, migrate=False)  # noqa: SLF001 - shared guarded definer
        rows = await async_dal.select_async(dal(dal.hub_users.id == user_id), dal.hub_users.uuid)
        user_uuid = str(rows.first().uuid) if rows else None
        if user_uuid is None:
            raise LookupError("hub user not found for audit")
        await async_dal.insert_async(
            dal.audit_log,
            user_id=user_id,
            action=action,
            target_type="hub_user",
            target_id=user_uuid,
            details={"user_uuid": user_uuid, "platform": platform, "actor": actor, "via": via},
            created_at=datetime.now(UTC),
        )
    except Exception as exc:
        _events.add(1, {"action": action, "outcome": "write_failed"})
        logger.error(
            "identity.audit.write_failed action=%s platform=%s error=%s",
            action,
            platform,
            type(exc).__name__,
            exc_info=True,
        )
        return
    _events.add(1, {"action": action, "outcome": "ok"})
    logger.info("identity.audit.recorded action=%s platform=%s", action, platform)
