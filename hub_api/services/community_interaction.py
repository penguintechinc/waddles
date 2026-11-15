"""Hub channels / forum / roles / permission-overrides service.

Port of Node's `interactionController.js`.

Largest single controller in the Community group (789 LOC in Node):
channel CRUD (with server/server-channel auto-provisioning), forum
post/reply CRUD with mirror-group relay, community role CRUD, and
per-channel permission overrides. Ported close to 1:1 -- see
`community_relay.py` for the relay leg.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .community_common import ensure_community_tables, sql_bool
from .community_relay import relay_message
from .scope_registry import (
    KIND_CUSTOM,
    KIND_FIXED,
    KIND_MODERATOR,
    effective_scopes,
    guard_delete,
    guard_grant,
    guard_scope_edit,
    normalize_scopes,
)

logger = logging.getLogger(__name__)


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


# ── Hub server auto-provisioning ───────────────────────────────────────────


def _ensure_hub_server(dal: Any, community_id: int) -> int:
    existing = (
        dal(
            (dal.community_servers.community_id == community_id)
            & (dal.community_servers.platform == "hub")
        )
        .select()
        .first()
    )
    if existing:
        return int(existing.id)
    new_id = dal.community_servers.insert(
        community_id=community_id,
        platform="hub",
        platform_server_id=f"hub-{community_id}",
        platform_server_name="Hub",
        status="approved",
        created_at=datetime.utcnow(),
    )
    dal.commit()
    return int(new_id)


def _create_server_channel(
    dal: Any, hub_server_id: int, hub_channel_id: int, name: str, channel_type: str
) -> int:
    new_id = dal.community_server_channels.insert(
        community_server_id=hub_server_id,
        platform_channel_id=f"hub-ch-{hub_channel_id}",
        platform_channel_name=name,
        channel_type=channel_type,
        created_at=datetime.utcnow(),
    )
    dal.commit()
    return int(new_id)


# ── Hub channel CRUD ─────────────────────────────────────────────────────


def _channel_dto(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "community_id": row.community_id,
        "name": row.name,
        "description": row.description,
        "channel_type": row.channel_type,
        "sort_order": row.sort_order,
        "is_active": bool(row.is_active),
        "allow_ad_hoc_voice": bool(row.allow_ad_hoc_voice),
        "has_chat": bool(row.has_chat),
        "has_voice": bool(row.has_voice),
        "has_video": bool(row.has_video),
        "is_temporary": bool(row.is_temporary),
        "temp_duration_minutes": row.temp_duration_minutes,
        "is_broadcast": bool(row.is_broadcast),
        "community_server_channel_id": row.community_server_channel_id,
        "created_at": _iso(row.created_at),
    }


def get_hub_channels(dal: Any, community_id: int) -> list[dict[str, Any]]:
    """Active hub channels for a community, ordered by type/sort/name."""
    ensure_community_tables(dal)
    rows = dal(
        (dal.hub_channels.community_id == community_id) & (dal.hub_channels.is_active == True)  # noqa: E712
    ).select(
        orderby=dal.hub_channels.channel_type | dal.hub_channels.sort_order | dal.hub_channels.name
    )
    return [_channel_dto(r) for r in rows]


def can_create_channel(dal: Any, community_id: int, user_id: int, is_admin_like: bool) -> bool:
    """Whether `user_id` may self-create a channel, per the community's channel-creation policy."""
    if is_admin_like:
        return True
    ensure_community_tables(dal)
    row = dal.executesql(
        """
        SELECT c.config, cr.base_claims, cm.claims_cache
        FROM communities c
        LEFT JOIN community_members cm
          ON cm.community_id = c.id AND cm.user_id = $2 AND cm.is_active = true
        LEFT JOIN community_roles cr ON cr.id = cm.community_role_id
        WHERE c.id = $1
        """,
        placeholders=[community_id, str(user_id)],
    )
    if not row or (row[0][1] is None and row[0][2] is None):
        return False
    config, base_claims, claims_cache = row[0]
    if claims_cache:
        scopes = claims_cache if isinstance(claims_cache, list) else claims_cache.get("scopes", [])
    else:
        scopes = (base_claims or {}).get("scopes", [])
    policy = (config or {}).get("channel_creation_policy", "admin_only")
    if policy == "all_members":
        return True
    if policy == "communicator":
        return "channels:create" in scopes or "community:manage_channels" in scopes
    return "community:manage_channels" in scopes or "community:manage_members" in scopes


def create_hub_channel(
    dal: Any, community_id: int, payload: dict[str, Any], user_id: int | None
) -> tuple[dict[str, Any] | None, str | None]:
    """Create a hub channel + auto-provision its bridged server/server-channel."""
    ensure_community_tables(dal)
    name = (payload.get("name") or "").strip()
    if not name:
        return None, "Channel name is required"
    channel_type = payload.get("channel_type", "chat")
    if channel_type not in {"chat", "forum", "voice"}:
        return None, "Invalid channel type"

    has_chat = payload.get("has_chat", channel_type in {"chat", "forum"})
    has_voice = payload.get("has_voice", channel_type == "voice")
    has_video = payload.get("has_video", False)
    is_temporary = payload.get("is_temporary", False)
    is_broadcast = payload.get("is_broadcast", False)

    existing = (
        dal((dal.hub_channels.community_id == community_id) & (dal.hub_channels.name == name))
        .select()
        .first()
    )
    if existing:
        return None, "A channel with that name already exists"

    channel_id = dal.hub_channels.insert(
        community_id=community_id,
        name=name,
        description=payload.get("description") or "",
        channel_type=channel_type,
        sort_order=payload.get("sort_order", 0),
        allow_ad_hoc_voice=payload.get("allow_ad_hoc_voice", False),
        has_chat=has_chat,
        has_voice=has_voice,
        has_video=has_video,
        is_temporary=is_temporary,
        temp_duration_minutes=payload.get("temp_duration_minutes"),
        is_broadcast=is_broadcast,
        created_by=user_id,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    dal.commit()

    hub_server_id = _ensure_hub_server(dal, community_id)
    server_channel_id = _create_server_channel(dal, hub_server_id, channel_id, name, channel_type)
    dal(dal.hub_channels.id == channel_id).update(community_server_channel_id=server_channel_id)
    dal.commit()

    if is_broadcast:
        member_role = (
            dal(
                (dal.community_roles.community_id == community_id)
                & (dal.community_roles.name == "member")
            )
            .select()
            .first()
        )
        if member_role:
            dal.hub_channel_permission_overrides.insert(
                hub_channel_id=channel_id,
                community_role_id=member_role.id,
                deny_scopes=["channels:send_chat", "channels:speak"],
                grant_scopes=[],
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            dal.commit()

    return _channel_dto(dal.hub_channels[channel_id]), None


def update_hub_channel(
    dal: Any, community_id: int, channel_id: int, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Partial update of a hub channel's mutable fields."""
    ensure_community_tables(dal)
    query = (dal.hub_channels.id == channel_id) & (dal.hub_channels.community_id == community_id)
    if dal(query).select().first() is None:
        return None

    fields: dict[str, Any] = {"updated_at": datetime.utcnow()}
    mutable = (
        "name",
        "description",
        "sort_order",
        "allow_ad_hoc_voice",
        "has_chat",
        "has_voice",
        "has_video",
        "is_temporary",
        "temp_duration_minutes",
        "is_broadcast",
    )
    for key in mutable:
        if key in payload:
            value = payload[key]
            fields[key] = value.strip() if key == "name" and isinstance(value, str) else value
    dal(query).update(**fields)
    dal.commit()
    return _channel_dto(dal.hub_channels[channel_id])


def delete_hub_channel(dal: Any, community_id: int, channel_id: int) -> bool:
    """Soft-delete (deactivate) a hub channel."""
    ensure_community_tables(dal)
    query = (dal.hub_channels.id == channel_id) & (dal.hub_channels.community_id == community_id)
    if dal(query).select().first() is None:
        return False
    dal(query).update(is_active=False, updated_at=datetime.utcnow())
    dal.commit()
    return True


# ── Forum CRUD ───────────────────────────────────────────────────────────


def _post_summary_dto(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "hub_channel_id": row.hub_channel_id,
        "title": row.title,
        "body": row.body,
        "tags": row.tags or [],
        "author_hub_user_id": row.author_hub_user_id,
        "author_platform": row.author_platform,
        "author_username": row.author_username,
        "author_avatar_url": row.author_avatar_url,
        "is_pinned": bool(row.is_pinned),
        "is_locked": bool(row.is_locked),
        "reply_count": row.reply_count,
        "last_reply_at": _iso(row.last_reply_at),
        "created_at": _iso(row.created_at),
    }


def get_forum_posts(
    dal: Any, community_id: int, channel_id: int, *, page: int, limit: int
) -> tuple[list[dict[str, Any]], int]:
    """Paginated forum posts for one channel, pinned-first then newest-first."""
    ensure_community_tables(dal)
    query = (dal.hub_forum_posts.hub_channel_id == channel_id) & (
        dal.hub_forum_posts.community_id == community_id
    )
    total = dal(query).count()
    offset = (page - 1) * limit
    rows = dal(query).select(
        orderby=(
            ~dal.hub_forum_posts.is_pinned,
            ~dal.hub_forum_posts.last_reply_at,
            ~dal.hub_forum_posts.created_at,
        ),
        limitby=(offset, offset + limit),
    )
    return [_post_summary_dto(r) for r in rows], total


def get_forum_post(dal: Any, community_id: int, post_id: int) -> dict[str, Any] | None:
    """One forum post with its replies, or `None` if not found."""
    ensure_community_tables(dal)
    row = (
        dal(
            (dal.hub_forum_posts.id == post_id) & (dal.hub_forum_posts.community_id == community_id)
        )
        .select()
        .first()
    )
    if row is None:
        return None
    replies = dal(dal.hub_forum_replies.post_id == post_id).select(
        orderby=dal.hub_forum_replies.created_at
    )
    post = _post_summary_dto(row)
    post["replies"] = [
        {
            "id": r.id,
            "author_hub_user_id": r.author_hub_user_id,
            "author_platform": r.author_platform,
            "author_username": r.author_username,
            "author_avatar_url": r.author_avatar_url,
            "content": r.content,
            "created_at": _iso(r.created_at),
        }
        for r in replies
    ]
    return post


async def create_forum_post(
    dal: Any, community_id: int, channel_id: int, payload: dict[str, Any], user: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """Create a forum post, then best-effort relay it to bridged channels."""
    ensure_community_tables(dal)
    title = (payload.get("title") or "").strip()
    if not title:
        return None, "Title is required"

    post_id = dal.hub_forum_posts.insert(
        hub_channel_id=channel_id,
        community_id=community_id,
        title=title,
        body=payload.get("body") or "",
        tags=payload.get("tags") or [],
        author_hub_user_id=user.get("user_id"),
        author_platform="hub",
        author_username=user.get("username"),
        author_avatar_url=user.get("avatar_url"),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    dal.commit()
    row = dal.hub_forum_posts[post_id]

    channel = dal.hub_channels[channel_id]
    if channel and channel.community_server_channel_id:
        await relay_message(
            dal,
            source_member_channel_id=channel.community_server_channel_id,
            platform="hub",
            channel_type="forum",
            content={
                "title": title,
                "body": payload.get("body") or "",
                "tags": payload.get("tags") or [],
            },
            author={
                "username": user.get("username"),
                "avatarUrl": user.get("avatar_url"),
                "platform": "hub",
            },
            message_type="forum_post",
        )
    return {"id": row.id, "title": row.title, "created_at": _iso(row.created_at)}, None


async def create_forum_reply(
    dal: Any, community_id: int, post_id: int, payload: dict[str, Any], user: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """Create a forum reply, bump the post's reply counter, then best-effort relay it."""
    ensure_community_tables(dal)
    content = (payload.get("content") or "").strip()
    if not content:
        return None, "Reply content is required"

    post = dal.executesql(
        """
        SELECT p.id, p.is_locked, p.platform_thread_id, hc.community_server_channel_id
        FROM hub_forum_posts p JOIN hub_channels hc ON hc.id = p.hub_channel_id
        WHERE p.id = $1 AND p.community_id = $2
        """,
        placeholders=[post_id, community_id],
    )
    if not post:
        return None, "__not_found__"
    _, is_locked, platform_thread_id, server_channel_id = post[0]
    if sql_bool(is_locked):
        return None, "This post is locked"

    reply_id = dal.hub_forum_replies.insert(
        post_id=post_id,
        author_hub_user_id=user.get("user_id"),
        author_platform="hub",
        author_username=user.get("username"),
        author_avatar_url=user.get("avatar_url"),
        content=content,
        created_at=datetime.utcnow(),
    )
    dal(dal.hub_forum_posts.id == post_id).update(
        reply_count=dal.hub_forum_posts.reply_count + 1, last_reply_at=datetime.utcnow()
    )
    dal.commit()
    row = dal.hub_forum_replies[reply_id]

    if server_channel_id:
        await relay_message(
            dal,
            source_member_channel_id=server_channel_id,
            platform="hub",
            channel_type="forum",
            content={"text": content, "platformThreadId": platform_thread_id},
            author={
                "username": user.get("username"),
                "avatarUrl": user.get("avatar_url"),
                "platform": "hub",
            },
            message_type="forum_reply",
        )
    return {"id": row.id, "content": row.content, "created_at": _iso(row.created_at)}, None


def moderate_forum_post(
    dal: Any, community_id: int, post_id: int, payload: dict[str, Any]
) -> str | None:
    """Pin/lock/delete a post. Returns `None` on success, else an error string."""
    ensure_community_tables(dal)
    query = (dal.hub_forum_posts.id == post_id) & (dal.hub_forum_posts.community_id == community_id)
    if payload.get("delete"):
        dal(query).delete()
        dal.commit()
        return None

    fields: dict[str, Any] = {}
    if "is_pinned" in payload:
        fields["is_pinned"] = payload["is_pinned"]
    if "is_locked" in payload:
        fields["is_locked"] = payload["is_locked"]
    if not fields:
        return "No moderation action specified"
    fields["updated_at"] = datetime.utcnow()
    dal(query).update(**fields)
    dal.commit()
    return None


def delete_forum_reply(dal: Any, community_id: int, reply_id: int) -> bool:
    """Delete a reply and decrement its post's reply counter."""
    ensure_community_tables(dal)
    row = dal.executesql(
        """
        SELECT r.post_id FROM hub_forum_replies r
        JOIN hub_forum_posts p ON r.post_id = p.id
        WHERE r.id = $1 AND p.community_id = $2
        """,
        placeholders=[reply_id, community_id],
    )
    if not row:
        return False
    post_id = row[0][0]
    dal(dal.hub_forum_replies.id == reply_id).delete()
    post = dal.hub_forum_posts[post_id]
    dal(dal.hub_forum_posts.id == post_id).update(reply_count=max(post.reply_count - 1, 0))
    dal.commit()
    return True


# ── Internal relay endpoint ──────────────────────────────────────────────


async def internal_relay_incoming(dal: Any, payload: dict[str, Any]) -> str | None:
    """Relay an inbound cross-platform message. Returns an error string, or `None`."""
    ensure_community_tables(dal)
    source_platform_channel_id = payload.get("sourcePlatformChannelId")
    row = (
        dal(dal.community_server_channels.platform_channel_id == source_platform_channel_id)
        .select()
        .first()
    )
    if row is None:
        return "Source channel not found"

    author = payload.get("author") or {}
    platform = payload.get("platform") or author.get("platform") or ""
    await relay_message(
        dal,
        source_member_channel_id=row.id,
        platform=str(platform),
        channel_type=payload.get("channelType") or "chat",
        content=payload.get("content") or {},
        author=author,
        message_type=payload.get("messageType") or "message",
    )
    return None


# ── Community roles CRUD ─────────────────────────────────────────────────


def role_kind_of(row: Any) -> int:
    """Effective `role_kind` of a role row; a kind-less `is_system` row counts as FIXED."""
    kind = getattr(row, "role_kind", None) or KIND_CUSTOM
    if kind == KIND_CUSTOM and row.is_system:
        return KIND_FIXED
    return int(kind)


def _role_dto(row: Any) -> dict[str, Any]:
    claims = row.base_claims or {}
    kind = role_kind_of(row)
    scopes = sorted(effective_scopes(kind, claims.get("scopes", [])))
    return {
        "id": row.id,
        "community_id": row.community_id,
        "name": row.name,
        "display_name": row.display_name,
        "description": row.description,
        "is_system": bool(row.is_system),
        "role_kind": kind,
        "priority": row.priority,
        "scopes": scopes,
        "scopes_editable": kind in (KIND_CUSTOM, KIND_MODERATOR),
        "deletable": kind == KIND_CUSTOM,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def get_community_roles(dal: Any, community_id: int) -> list[dict[str, Any]]:
    """All roles for a community, highest priority first."""
    ensure_community_tables(dal)
    rows = dal(dal.community_roles.community_id == community_id).select(
        orderby=(~dal.community_roles.priority, dal.community_roles.name)
    )
    return [_role_dto(r) for r in rows]


def _log_role_change(
    action: str, community_id: int, role_id: int, scopes: list[str] | None
) -> None:
    """Structured, PII-free trace of a role mutation (the audit chain row is written per-route)."""
    logger.info(
        "community_role.%s community_id=%s role_id=%s scope_count=%s",
        action,
        community_id,
        role_id,
        "n/a" if scopes is None else len(scopes),
    )


def create_community_role(
    dal: Any,
    community_id: int,
    payload: dict[str, Any],
    *,
    editor_scopes: frozenset[str],
) -> tuple[dict[str, Any] | None, str | None]:
    """Create a custom role. Priority is capped 0-49; granted scopes must be held by the editor."""
    ensure_community_tables(dal)
    name = (payload.get("name") or "").strip()
    if not name:
        return None, "Role name is required"
    priority = payload.get("priority", 0)
    if priority is not None and not (0 <= priority <= 49):
        return None, "Custom role priority must be between 0 and 49"
    scopes = normalize_scopes(payload.get("scopes") or [])
    guard_grant(editor_scopes, scopes)

    existing = (
        dal(
            (dal.community_roles.community_id == community_id)
            & (dal.community_roles.name == name.lower())
        )
        .select()
        .first()
    )
    if existing:
        return None, "A role with that name already exists"

    role_id = dal.community_roles.insert(
        community_id=community_id,
        name=name.lower(),
        display_name=payload.get("displayName") or name,
        description=payload.get("description") or "",
        is_system=False,
        role_kind=KIND_CUSTOM,
        priority=priority or 0,
        base_claims={"scopes": scopes},
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    dal.commit()
    _log_role_change("created", community_id, role_id, scopes)
    return _role_dto(dal.community_roles[role_id]), None


def update_community_role(
    dal: Any,
    community_id: int,
    role_id: int,
    payload: dict[str, Any],
    *,
    editor_scopes: frozenset[str],
) -> str | None:
    """Update a role under the role-kind guards.

    Admin-kind scopes are never editable (always the full registry); fixed predefined roles keep
    their scopes; the moderator-kind and custom roles accept a scope edit, but only for scopes
    the editor holds (a scope the role already carries and the editor lacks is left untouched).
    """
    ensure_community_tables(dal)
    existing = (
        dal(
            (dal.community_roles.id == role_id) & (dal.community_roles.community_id == community_id)
        )
        .select()
        .first()
    )
    if existing is None:
        return "__not_found__"
    kind = role_kind_of(existing)

    fields: dict[str, Any] = {"updated_at": datetime.utcnow()}
    if "displayName" in payload:
        fields["display_name"] = payload["displayName"]
    if "description" in payload:
        fields["description"] = payload["description"]
    new_scopes: list[str] | None = None
    if "scopes" in payload:
        guard_scope_edit(kind)
        new_scopes = normalize_scopes(payload["scopes"])
        current = set(effective_scopes(kind, (existing.base_claims or {}).get("scopes", [])))
        # No downgrading a role more powerful than the editor: they must hold every current scope.
        guard_grant(editor_scopes, current)
        guard_grant(editor_scopes, set(new_scopes) - current)
        fields["base_claims"] = {"scopes": new_scopes}
    if "priority" in payload and kind == KIND_CUSTOM:
        fields["priority"] = payload["priority"]

    dal(dal.community_roles.id == role_id).update(**fields)
    if new_scopes is not None:
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.community_role_id == role_id)
        ).update(claims_cache=None)
    dal.commit()
    _log_role_change("updated", community_id, role_id, new_scopes)
    return None


def delete_community_role(
    dal: Any, community_id: int, role_id: int, *, editor_scopes: frozenset[str]
) -> str | None:
    """Delete a custom role (members move to `member`); predefined roles are refused (409)."""
    ensure_community_tables(dal)
    existing = (
        dal(
            (dal.community_roles.id == role_id) & (dal.community_roles.community_id == community_id)
        )
        .select()
        .first()
    )
    if existing is None:
        return "__not_found__"
    kind = role_kind_of(existing)
    guard_delete(kind)
    # No deleting a role more powerful than the editor: they must hold every scope it carries.
    guard_grant(
        editor_scopes, effective_scopes(kind, (existing.base_claims or {}).get("scopes", []))
    )

    member_role = (
        dal(
            (dal.community_roles.community_id == community_id)
            & (dal.community_roles.name == "member")
        )
        .select()
        .first()
    )
    if member_role:
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.community_role_id == role_id)
        ).update(community_role_id=member_role.id, claims_cache=None)
    dal(dal.community_roles.id == role_id).delete()
    dal.commit()
    _log_role_change("deleted", community_id, role_id, None)
    return None


# ── Channel permission overrides ─────────────────────────────────────────


def get_channel_permission_overrides(
    dal: Any, community_id: int, channel_id: int
) -> list[dict[str, Any]] | None:
    """Overrides for one channel, or `None` if the channel doesn't belong to the community."""
    ensure_community_tables(dal)
    if (
        dal((dal.hub_channels.id == channel_id) & (dal.hub_channels.community_id == community_id))
        .select()
        .first()
        is None
    ):
        return None
    rows = dal.executesql(
        """
        SELECT o.id, o.hub_channel_id, o.community_role_id, cr.name, cr.display_name,
               o.grant_scopes, o.deny_scopes, o.scope, o.created_at
        FROM hub_channel_permission_overrides o
        JOIN community_roles cr ON cr.id = o.community_role_id
        WHERE o.hub_channel_id = $1
        ORDER BY cr.priority DESC
        """,
        placeholders=[channel_id],
    )
    return [
        {
            "id": r[0],
            "hub_channel_id": r[1],
            "community_role_id": r[2],
            "role_name": r[3],
            "role_display_name": r[4],
            "grant_scopes": r[5] or [],
            "deny_scopes": r[6] or [],
            "scope": r[7],
            "created_at": _iso(r[8]),
        }
        for r in rows
    ]


def update_channel_permission_overrides(
    dal: Any, community_id: int, channel_id: int, overrides: list[dict[str, Any]]
) -> bool:
    """Replace all overrides for one channel; invalidates every member's claims cache."""
    ensure_community_tables(dal)
    if (
        dal((dal.hub_channels.id == channel_id) & (dal.hub_channels.community_id == community_id))
        .select()
        .first()
        is None
    ):
        return False

    dal(dal.hub_channel_permission_overrides.hub_channel_id == channel_id).delete()
    for override in overrides:
        role_id = override.get("communityRoleId")
        if not role_id:
            continue
        dal.hub_channel_permission_overrides.insert(
            hub_channel_id=channel_id,
            community_role_id=role_id,
            grant_scopes=override.get("grantScopes") or [],
            deny_scopes=override.get("denyScopes") or [],
            scope=override.get("scope", "both"),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
    dal(dal.community_members.community_id == community_id).update(claims_cache=None)
    dal.commit()
    return True
