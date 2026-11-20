"""Mirror-group relay -- port of Node's `services/mirrorRelayService.js`.

Fans a message/forum-post/forum-reply out to every other active member
of the mirror group(s) the source channel belongs to. The `messageType
== "message"` hub-target branch (Socket.IO room broadcast) is not
ported -- it depends on the same live realtime path
`blueprints/v1/community_chat.py` documents as out of scope for this PR
(mounting `python-socketio` requires an `app.py` change this porting
wave must not make). The `forum_post`/`forum_reply` hub-target branches
are pure DB writes and are ported in full; external-platform dispatch
(Discord/Slack/Teams/Mattermost/Google Chat bot relay endpoints) is
ported as a best-effort, error-swallowing HTTP POST, matching Node's
`Promise.allSettled` fire-and-forget semantics.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import httpx

from .community_common import ensure_community_tables
from .sql_util import execute_positional

_RELAY_URLS = {
    "discord": os.getenv("DISCORD_BOT_RELAY_URL", "http://discord-bot-service:8080/internal/relay"),
    "slack": os.getenv("SLACK_BOT_RELAY_URL", "http://slack-bot-service:8081/internal/relay"),
    "teams": os.getenv(
        "TEAMS_BOT_RELAY_URL", "http://waddlebot-teams-collector:8008/internal/relay"
    ),
    "mattermost": os.getenv(
        "MATTERMOST_BOT_RELAY_URL", "http://waddlebot-mattermost-collector:8009/internal/relay"
    ),
    "googlechat": os.getenv(
        "GOOGLECHAT_BOT_RELAY_URL", "http://waddlebot-googlechat-collector:8012/internal/relay"
    ),
}
_RELAY_TIMEOUT_SECONDS = 5.0


def _ensure_relay_tables(dal: Any, *, migrate: bool = False) -> None:
    ensure_community_tables(dal, migrate=migrate)
    if "mirror_groups" not in dal.tables:
        dal.define_table(
            "mirror_groups",
            dal.Field("community_id", "integer", notnull=True),
            dal.Field("channel_type", "string", default="chat"),
            dal.Field("is_active", "boolean", default=True),
            migrate=migrate,
        )
    if "mirror_group_members" not in dal.tables:
        dal.define_table(
            "mirror_group_members",
            dal.Field("mirror_group_id", "integer", notnull=True),
            dal.Field("community_server_id", "integer", notnull=True),
            dal.Field("community_server_channel_id", "integer"),
            dal.Field("direction", "string", default="both"),
            dal.Field("is_active", "boolean", default=True),
            migrate=migrate,
        )


async def _dispatch_to_hub(
    dal: Any,
    target: tuple[Any, ...],
    content: dict[str, Any],
    author: dict[str, Any],
    message_type: str,
) -> None:
    hub_channel_id, community_id = target[5], target[6]
    if hub_channel_id is None:
        return
    if message_type == "forum_post":
        dal.hub_forum_posts.insert(
            hub_channel_id=hub_channel_id,
            community_id=community_id,
            title=content.get("title"),
            body=content.get("body") or "",
            tags=content.get("tags") or [],
            author_platform=author.get("platform"),
            author_username=author.get("username"),
            author_avatar_url=author.get("avatarUrl"),
            platform_thread_id=content.get("platformThreadId"),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        dal.commit()
    elif message_type == "forum_reply":
        post = (
            dal(
                (dal.hub_forum_posts.hub_channel_id == hub_channel_id)
                & (dal.hub_forum_posts.platform_thread_id == content.get("platformThreadId"))
            )
            .select()
            .first()
        )
        if post is None:
            return
        dal.hub_forum_replies.insert(
            post_id=post.id,
            author_platform=author.get("platform"),
            author_username=author.get("username"),
            author_avatar_url=author.get("avatarUrl"),
            content=content.get("text"),
            platform_message_id=content.get("platformMessageId"),
            created_at=datetime.utcnow(),
        )
        dal(dal.hub_forum_posts.id == post.id).update(
            reply_count=post.reply_count + 1, last_reply_at=datetime.utcnow()
        )
        dal.commit()
    # message_type == "message": requires the Socket.IO room broadcast this
    # port doesn't mount yet -- intentionally not persisted here either,
    # matching "no live send path" documented in community_chat.py.


async def _dispatch_to_platform_bot(
    platform: str,
    target: tuple[Any, ...],
    content: dict[str, Any],
    author: dict[str, Any],
    message_type: str,
) -> None:
    relay_url = _RELAY_URLS.get(platform)
    if relay_url is None:
        return
    body = {
        "platformChannelId": target[2],
        "channelName": target[3],
        "content": content,
        "author": author,
        "messageType": message_type,
    }
    try:
        async with httpx.AsyncClient(timeout=_RELAY_TIMEOUT_SECONDS) as client:
            await client.post(relay_url, json=body)
    except httpx.RequestError:
        pass  # fire-and-forget, matches Node's Promise.allSettled swallow


async def relay_message(
    dal: Any,
    *,
    source_member_channel_id: int,
    platform: str,
    channel_type: str,
    content: dict[str, Any],
    author: dict[str, Any],
    message_type: str = "message",
    exclude_target_id: int | None = None,
) -> None:
    """Fan a message out to every other active member of the source channel's mirror group(s)."""
    _ensure_relay_tables(dal)
    groups = execute_positional(
        dal,
        """
        SELECT DISTINCT mg.id
        FROM mirror_group_members mgm
        JOIN mirror_groups mg ON mg.id = mgm.mirror_group_id
        WHERE mgm.community_server_channel_id = $1 AND mgm.is_active = true AND mg.channel_type = $2
        """,
        placeholders=[source_member_channel_id, channel_type],
    )
    if not groups:
        return
    group_ids = [row[0] for row in groups]

    targets = execute_positional(
        dal,
        """
        SELECT mgm.community_server_channel_id, cs.platform,
               csc.platform_channel_id, csc.platform_channel_name,
               mgm.direction, hc.id AS hub_channel_id, hc.community_id
        FROM mirror_group_members mgm
        JOIN community_server_channels csc ON csc.id = mgm.community_server_channel_id
        JOIN community_servers cs ON cs.id = csc.community_server_id
        LEFT JOIN hub_channels hc ON hc.community_server_channel_id = csc.id
        WHERE mgm.mirror_group_id = ANY($1) AND mgm.is_active = true
          AND mgm.community_server_channel_id != $2
        """,
        placeholders=[group_ids, source_member_channel_id],
    )

    is_from_hub = platform == "hub"
    for target in targets:
        target_channel_id, target_platform, _, _, direction = (
            target[0],
            target[1],
            target[2],
            target[3],
            target[4],
        )
        if exclude_target_id and target_channel_id == exclude_target_id:
            continue
        if is_from_hub and direction == "to_hub":
            continue
        if not is_from_hub and direction == "from_hub":
            continue

        if target_platform == "hub":
            await _dispatch_to_hub(dal, target, content, author, message_type)
        else:
            await _dispatch_to_platform_bot(target_platform, target, content, author, message_type)
