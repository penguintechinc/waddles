"""Member-facing community self-create and recent-activity feed.

Self-create CALLS `superadmin_service.create_community` (name validation, free-tier cap, role
seeding and the creator's link to the seeded owner/admin role all live there). Authority is always
the role's scopes, never a role name.

The activity feed merges `community_members.joined_at` (active, non-banned members) with
published announcements, newest first, exposing hub user public UUIDs only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from services import superadmin_service
from services.community_common import ensure_community_tables
from services.membership_state import MembershipState

#: PostHog flag gating self-create; default OFF until validated.
FEATURE_COMMUNITY_SELF_CREATE: Final = "waddles.community-self-create"

KIND_MEMBER_JOINED: Final = 1
KIND_ANNOUNCEMENT: Final = 2


@dataclass(slots=True, frozen=True)
class ActivityItem:
    """One feed entry; `kind` is the int enum above, ids are public UUIDs (never numeric)."""

    kind: int
    at: str
    actorId: str | None  # noqa: N815 - wire contract is camelCase
    actorName: str | None  # noqa: N815
    title: str | None


async def create_own_community(
    async_dal: Any,
    dal: Any,
    *,
    caller_id: int,
    tenant_slug: str,
    tenant_id: int,
    name: str,
    display_name: str | None,
    description: str | None,
    platform: str,
    community_type: str | None,
) -> Any:
    """Create a community in the caller's tenant and make the caller its admin-role holder."""
    community = await superadmin_service.create_community(
        async_dal,
        dal,
        name=name,
        display_name=display_name,
        description=description,
        platform=platform,
        platform_server_id=None,  # server binding only via the verified pairing/bot-install flow
        is_public=None,
        community_type=community_type,
        caller_id=caller_id,
        tenant_slug=tenant_slug,
        tenant_id=tenant_id,
    )
    return community


def _iso(value: Any) -> str:
    """ISO8601 for a datetime (sqlite may hand back a str)."""
    return str(value.isoformat() if hasattr(value, "isoformat") else value)


def list_activity(dal: Any, *, community_id: int, limit: int) -> list[ActivityItem]:
    """Newest-first merge of member joins and published announcements (<= `limit`)."""
    ensure_community_tables(dal)
    members = dal.community_members
    not_banned = (members.membership_state == None) | (  # noqa: E711
        members.membership_state != int(MembershipState.BANNED)
    )
    joins = dal(
        (members.community_id == community_id)
        & (members.is_active == True)  # noqa: E712
        & (members.joined_at != None)  # noqa: E711
        & not_banned
    ).select(orderby=~members.joined_at, limitby=(0, limit))
    ann = dal.announcements
    posts = dal((ann.community_id == community_id) & (ann.status == "published")).select(
        orderby=~ann.published_at, limitby=(0, limit)
    )
    user_ids = {int(m.user_id) for m in joins if str(m.user_id).isdigit()}
    user_ids |= {int(a.created_by) for a in posts if a.created_by is not None}
    users: dict[int, tuple[str | None, str | None]] = {}
    if user_ids:
        hu = dal.hub_users
        for row in dal(hu.id.belongs(sorted(user_ids))).select(hu.id, hu.uuid, hu.username):
            users[int(row.id)] = (str(row.uuid) if row.uuid else None, row.username)

    def actor(raw: Any) -> tuple[str | None, str | None]:
        if raw is None or not str(raw).isdigit():
            return None, None
        return users.get(int(raw), (None, None))

    entries: list[ActivityItem] = []
    for m in joins:
        uid, uname = actor(m.user_id)
        entries.append(
            ActivityItem(KIND_MEMBER_JOINED, _iso(m.joined_at), uid, uname or m.display_name, None)
        )
    for a in posts:
        when = a.published_at or a.created_at
        if when is None:
            continue
        uid, uname = actor(a.created_by)
        entries.append(
            ActivityItem(KIND_ANNOUNCEMENT, _iso(when), uid, uname or a.created_by_name, a.title)
        )
    entries.sort(key=lambda e: e.at, reverse=True)
    return entries[:limit]
