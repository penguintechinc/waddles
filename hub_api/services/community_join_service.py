"""Direct join for open-policy communities (`POST /api/v1/communities/<id>/join`).

Approval-mode communities use `join_request_service`; this module is the open-join
counterpart. Tenant scoping comes from the caller's validated token, never the request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services import ban_suppression
from services.bundle_telemetry import get_meter
from services.errors import ApiError, not_found

logger = logging.getLogger(__name__)

_JOINS: Any = None


@dataclass(slots=True, frozen=True)
class JoinResult:
    """Outcome of an open-join call: `joined` is False when the caller was already a member."""

    community_id: int
    joined: bool


def _count(result: str) -> None:
    """Increment the join counter (telemetry failure never fails the request)."""
    global _JOINS
    try:
        if _JOINS is None:
            _JOINS = get_meter().create_counter(
                "waddles_hub_community_join_total",
                description="open community join attempts, by result",
            )
        _JOINS.add(1, {"result": result})
    except Exception:  # noqa: BLE001 - telemetry must never break the request
        logger.debug("community_join.metrics_failed", exc_info=True)


async def join_open_community(
    async_dal: Any, dal: Any, *, community_id: int, user_id: int, tenant_id: int
) -> JoinResult:
    """Make `user_id` a member of an open community in the caller's own tenant.

    404 if the community is absent or in another tenant; 403 `JOIN_NOT_OPEN` when the
    join policy is not `open`; 403 `MEMBERSHIP_RESTRICTED` for an inactive row or a
    ban-suppression match; an existing active membership is a 200 no-op.
    """
    rows = await async_dal.select_async(
        dal((dal.communities.id == community_id) & (dal.communities.tenant_id == tenant_id))
    )
    community = rows.first() if rows else None
    if community is None:
        _count("not_found")
        raise not_found("Community not found")
    if (community.join_mode or "open") != "open":
        _count("closed")
        raise ApiError("This community is not open to direct joining", 403, "JOIN_NOT_OPEN")

    members = await async_dal.select_async(
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.user_id == str(user_id))
        )
    )
    existing = members.first() if members else None
    if existing is not None:
        if existing.is_active is False:
            _count("restricted")
            raise ApiError("Membership is restricted", 403, "MEMBERSHIP_RESTRICTED")
        _count("already_member")
        return JoinResult(community_id=community_id, joined=False)

    match = await ban_suppression.check_and_ban_membership(
        async_dal, dal, community_id=community_id, hub_user_id=user_id
    )
    if match is not None:
        _count("restricted")
        raise ApiError("Membership is restricted", 403, "MEMBERSHIP_RESTRICTED")

    await async_dal.insert_async(
        dal.community_members,
        community_id=community_id,
        user_id=str(user_id),
        role="member",
        is_active=True,
        joined_at=datetime.now(UTC),
    )
    _count("joined")
    logger.info("community_join.joined", extra={"community_id": community_id})
    return JoinResult(community_id=community_id, joined=True)
