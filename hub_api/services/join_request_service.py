"""Community join requests -- ported from `joinRequestController.js`.

Submit/view-own actions are self-service (own resource, current user's own
request); list/approve/reject are admin actions gated by
`services.community_authz.require_community_scope` -- the same DB-backed
per-community check Node's `middleware/auth.js::requireCommunityAdmin`
performs at the route level (see `community_authz.py`'s own docstring for
why a flat JWT scope claim is not safe to use alone here). Node's own
`approveRequest`/`rejectRequest`/`listRequests` controllers have NO
additional in-controller check beyond the route middleware -- this port
matches that (unlike `community_profile_service.update_community_profile`,
which layers an extra owner/admin-only check on top).

`community_join_requests.user_id` is a real INTEGER FK to `hub_users.id`
(unlike the legacy VARCHAR `community_members.user_id`) -- see
`services/schema.py`'s table-binding comment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services import ban_suppression
from services.community_authz import require_community_scope
from services.errors import bad_request, conflict, forbidden, not_found

#: OR-check, mirrors `requireCommunityAdmin`'s own
#: `scopes.includes('community:manage_members') || scopes.includes('community:manage_channels')`.
_ADMIN_SCOPES = ("community:manage_members", "community:manage_channels")


@dataclass(slots=True, frozen=True)
class SubmittedJoinRequest:
    """Shaped like Node's raw `RETURNING id, status, created_at` -- snake_case, unmapped."""

    id: int
    status: str
    created_at: str


@dataclass(slots=True, frozen=True)
class MyJoinRequest:
    """Shaped like Node's raw `SELECT id, status, message, created_at, reviewed_at`."""

    id: int
    status: str
    message: str | None
    created_at: str
    reviewed_at: str | None


@dataclass(slots=True, frozen=True)
class JoinRequestListItem:
    """Shaped like Node's `listRequests` SELECT -- mixed casing preserved byte-for-byte.

    `avatar_url as "avatarUrl"` is the one explicitly-aliased camelCase
    column in Node's raw SQL; everything else stays snake_case/unaliased
    (`created_at`, `reviewed_at`) -- `AdminJoinRequests.jsx` reads
    `req.created_at` directly, so "fixing" this to all-camelCase would
    break the real frontend consumer.
    """

    id: int
    status: str
    message: str | None
    created_at: str
    reviewed_at: str | None
    username: str
    email: str
    avatarUrl: str | None


async def _get_community(dal: Any, async_dal: Any, community_id: int) -> Any:
    rows = await async_dal.select_async(dal(dal.communities.id == community_id))
    return rows.first() if rows else None


async def submit_request(
    async_dal: Any, dal: Any, *, community_id: int, user_id: int, message: str | None
) -> SubmittedJoinRequest:
    """Submit a join request (self-service, own resource)."""
    community = await _get_community(dal, async_dal, community_id)
    if community is None:
        raise not_found("Community not found")
    if (community.join_mode or "open") != "approval":
        raise bad_request("This community does not require approval to join")

    existing_member = await async_dal.select_async(
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.user_id == str(user_id))
        )
    )
    if existing_member:
        raise conflict("Already a member")

    now = datetime.now(UTC)
    existing_request = await async_dal.select_async(
        dal(
            (dal.community_join_requests.community_id == community_id)
            & (dal.community_join_requests.user_id == user_id)
        )
    )
    if existing_request:
        row = existing_request.first()
        await async_dal.update_async(
            dal.community_join_requests.id == row.id,
            status="pending",
            message=message,
            reviewed_by=None,
            reviewed_at=None,
        )
        request_id = row.id
        created_at = row.created_at or now
    else:
        request_id = await async_dal.insert_async(
            dal.community_join_requests,
            community_id=community_id,
            user_id=user_id,
            status="pending",
            message=message,
            created_at=now,
        )
        created_at = now

    return SubmittedJoinRequest(
        id=request_id,
        status="pending",
        created_at=created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
    )


async def get_my_request(
    async_dal: Any, dal: Any, *, community_id: int, user_id: int
) -> MyJoinRequest | None:
    """Get the caller's own join request status (self-service, own resource)."""
    rows = await async_dal.select_async(
        dal(
            (dal.community_join_requests.community_id == community_id)
            & (dal.community_join_requests.user_id == user_id)
        )
    )
    if not rows:
        return None
    row = rows.first()
    return MyJoinRequest(
        id=row.id,
        status=row.status,
        message=row.message,
        created_at=row.created_at.isoformat() if row.created_at else "",
        reviewed_at=row.reviewed_at.isoformat() if row.reviewed_at else None,
    )


async def list_requests(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    admin_user_id: int,
    status: str,
    page: int,
    limit: int,
) -> tuple[list[JoinRequestListItem], int]:
    """List join requests for a community (admin, `community:manage_members|_channels`)."""
    await require_community_scope(
        async_dal, dal, community_id=community_id, user_id=admin_user_id, any_of=_ADMIN_SCOPES
    )

    page = max(1, page)
    limit = min(50, max(1, limit))
    offset = (page - 1) * limit

    query = (dal.community_join_requests.community_id == community_id) & (
        dal.community_join_requests.status == status
    )
    total = await async_dal.count_async(query)
    rows = await async_dal.select_async(
        dal(query),
        dal.community_join_requests.ALL,
        dal.hub_users.ALL,
        left=dal.hub_users.on(dal.community_join_requests.user_id == dal.hub_users.id),
        orderby=dal.community_join_requests.created_at,
        limitby=(offset, offset + limit),
    )
    items = [
        JoinRequestListItem(
            id=r.community_join_requests.id,
            status=r.community_join_requests.status,
            message=r.community_join_requests.message,
            created_at=(
                r.community_join_requests.created_at.isoformat()
                if r.community_join_requests.created_at
                else ""
            ),
            reviewed_at=(
                r.community_join_requests.reviewed_at.isoformat()
                if r.community_join_requests.reviewed_at
                else None
            ),
            username=r.hub_users.username or "",
            email=r.hub_users.email or "",
            avatarUrl=r.hub_users.avatar_url,
        )
        for r in rows
    ]
    return items, int(total)


async def approve_request(
    async_dal: Any, dal: Any, *, community_id: int, request_id: int, reviewer_id: int
) -> None:
    """Approve a pending join request (admin, `community:manage_members|_channels`)."""
    await require_community_scope(
        async_dal, dal, community_id=community_id, user_id=reviewer_id, any_of=_ADMIN_SCOPES
    )

    rows = await async_dal.select_async(
        dal(
            (dal.community_join_requests.id == request_id)
            & (dal.community_join_requests.community_id == community_id)
            & (dal.community_join_requests.status == "pending")
        )
    )
    if not rows:
        raise not_found("Pending request not found")
    applicant_id = rows.first().user_id

    # Ban-evasion suppression: an applicant whose platform identity matches a non-expired
    # `suppression_records` row in THIS community's tenant (a banned user who erased their
    # account and came back) is not admitted -- the restriction is re-applied (membership
    # written banned + inactive) and the request is closed. Fails closed: a key/database
    # error propagates and the request stays pending rather than admitting a banned user.
    match = await ban_suppression.check_and_ban_membership(
        async_dal, dal, community_id=community_id, hub_user_id=applicant_id
    )
    if match is not None:
        await async_dal.update_async(
            dal.community_join_requests.id == request_id,
            status="rejected",
            reviewed_by=reviewer_id,
            reviewed_at=datetime.now(UTC),
        )
        raise forbidden("Applicant is restricted from joining this tenant")

    existing_member = await async_dal.select_async(
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.user_id == str(applicant_id))
        )
    )
    if not existing_member:
        await async_dal.insert_async(
            dal.community_members,
            community_id=community_id,
            user_id=str(applicant_id),
            role="member",
            is_active=True,
            joined_at=datetime.now(UTC),
        )

    await async_dal.update_async(
        dal.community_join_requests.id == request_id,
        status="approved",
        reviewed_by=reviewer_id,
        reviewed_at=datetime.now(UTC),
    )


async def reject_request(
    async_dal: Any, dal: Any, *, community_id: int, request_id: int, reviewer_id: int
) -> None:
    """Reject a pending join request (admin, `community:manage_members|_channels`)."""
    await require_community_scope(
        async_dal, dal, community_id=community_id, user_id=reviewer_id, any_of=_ADMIN_SCOPES
    )

    updated = await async_dal.update_async(
        (dal.community_join_requests.id == request_id)
        & (dal.community_join_requests.community_id == community_id)
        & (dal.community_join_requests.status == "pending"),
        status="rejected",
        reviewed_by=reviewer_id,
        reviewed_at=datetime.now(UTC),
    )
    if not updated:
        raise not_found("Pending request not found")
