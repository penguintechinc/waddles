"""Lift an automated floor ban (`bundle_reputation_floor_bans`) so the user regains normal access.

The outbox row of a reversed ban moves to the terminal LIFTED status (8, alembic 0139) with
`lifted_at` / `lifted_by` (the acting admin's hub UUID). Every ban CHECK reads
`ban_suppression.FLOOR_BAN_ACTIVE_STATUSES`, which excludes LIFTED, so a lifted user is treated
exactly as if never banned; a later ban (a fresh enqueue) is enforced again. hub-api holds no
UPDATE on the table, so the write is the SECURITY DEFINER function `reputation_floor_ban_lift`.

Floor bans are tenant-wide, so the lift is tenant-wide and its authorization is tenant-admin
(see `admin_service.authorize_floor_ban_lift`). Telemetry and logs carry counts only -- never an
identity token.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from typing import Any, Final

from services import ban_suppression
from services.bundle_telemetry import get_meter
from services.community_reputation_service import _admin_actor
from services.data_privacy_service import subject_uuids_of
from services.errors import not_found
from services.sql_util import execute_positional

logger = logging.getLogger(__name__)

#: `bundle_reputation_floor_bans.status` for a ban an admin reversed (terminal, never enforced).
FLOOR_BAN_LIFTED: Final = 8

_LIFT_SQL = "SELECT reputation_floor_ban_lift($1::bigint, $2::uuid[], $3::uuid)"

#: `(tenant_id, user_uuids, lifted_by_uuid) -> rows lifted`; the test seam for non-Postgres DALs.
LiftCall = Callable[[int, list[str], str], int]

_COUNTER: Any = None


def _emit(result: str) -> None:
    """Count one lift attempt by result (`lifted` / `none`); telemetry never fails the request."""
    global _COUNTER
    try:
        if _COUNTER is None:
            _COUNTER = get_meter().create_counter(
                "waddles_hub_floor_ban_lift_total",
                description="admin floor-ban lifts, by result",
            )
        _COUNTER.add(1, {"result": result})
    except Exception:  # noqa: BLE001 - telemetry must never fail a request
        logger.exception("floor_ban_lift.metrics_failed")


def _postgres_call(dal: Any) -> LiftCall:
    """The production call: the SECURITY DEFINER function over the request's DAL."""

    def call(tenant_id: int, user_uuids: list[str], lifted_by: str) -> int:
        rows = execute_positional(dal, _LIFT_SQL, placeholders=[tenant_id, user_uuids, lifted_by])
        return int(rows[0][0]) if rows else 0

    return call


def lift_floor_bans_sync(
    dal: Any,
    *,
    tenant_id: int,
    user_uuids: Collection[str],
    lifted_by: str,
    call: LiftCall | None = None,
) -> int:
    """Lift every in-force floor ban of these identity tokens in the tenant; returns rows lifted."""
    tokens = ban_suppression._well_formed_uuids(user_uuids)
    if not tokens:
        return 0
    lift = call or _postgres_call(dal)
    return lift(tenant_id, tokens, lifted_by)


async def lift_floor_bans(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    user_uuid: str,
    actor_user_id: int | None,
    call: LiftCall | None = None,
) -> int:
    """Lift the hub user's (public UUID) floor bans in the tenant; 404 when none was in force.

    The UUID is resolved server-side to the hub user; an unknown user and a user with no ban in
    this tenant are indistinguishable (both 404), so the route is no identity oracle.

    The audit event for the route is derived from the URL rule by `audit_events`; this adds the
    OTel counter and a value-free log line.
    """
    actor = await _admin_actor(async_dal, dal, actor_user_id)

    def work() -> int:
        users = dal.hub_users
        row = dal(users.uuid == user_uuid).select(users.id).first()
        if row is None:
            return 0
        tokens = subject_uuids_of(dal, user_id=int(row.id))
        return lift_floor_bans_sync(
            dal, tenant_id=tenant_id, user_uuids=tokens, lifted_by=actor, call=call
        )

    lifted = await ban_suppression.run_in_transaction(async_dal, dal, work)
    _emit("lifted" if lifted else "none")
    logger.info(
        "floor_ban.lifted",
        extra={
            "action": "floor_ban_lift",
            "result": "ok" if lifted else "none",
            "tenant_id": tenant_id,
            "rows": lifted,
        },
    )
    if not lifted:
        raise not_found("Member has no floor ban in force")
    return lifted
