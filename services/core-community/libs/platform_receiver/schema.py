"""Standard event schema constants and validators for platform receiver bots.

Centralises the event dict structure so all platform bots produce identical
payloads.  The router and modules expect this exact schema.
"""
from typing import Any, Dict, Optional


# Valid message_type values
MESSAGE_TYPES = frozenset({
    "chatMessage",
    "slashCommand",
    "interaction",
    "modal_submit",
    "button_click",
    "select_menu",
    "mention",
    "shortcut",
    "stream_online",
    "stream_offline",
    "subscription",
    "gift_subscription",
    "follow",
    "raid",
    "cheer",
    "presence_update",
})

# Platforms with native slash-command UIs (can open modals, ephemeral replies, etc.)
# - discord/slack: native slash commands
# - teams: message extensions + bot commands
# - mattermost: native slash commands
# - googlechat: slash command configuration
SLASH_COMMAND_PLATFORMS = frozenset({"discord", "slack", "teams", "mattermost", "googlechat"})

# Platforms that use ! prefix chat commands (IRC-style)
PREFIX_COMMAND_PLATFORMS = frozenset({"twitch", "kick", "youtube"})

# Platforms that support presence/status updates
PRESENCE_PLATFORMS = frozenset({"slack", "discord", "teams", "mattermost", "googlechat"})


def build_event(
    *,
    platform: str,
    message_type: str,
    user_id: str,
    username: str,
    display_name: str,
    content: str,
    channel_id: str,
    server_id: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a fully validated standard event dict.

    Raises ValueError if ``message_type`` is not recognised.
    """
    if message_type not in MESSAGE_TYPES:
        raise ValueError(
            f"Unknown message_type '{message_type}'. Valid types: {sorted(MESSAGE_TYPES)}"
        )
    entity_id = (
        f"{server_id}:{channel_id}" if server_id and server_id != channel_id else channel_id
    )
    return {
        "entity_id": entity_id,
        "user_id": user_id,
        "username": username,
        "display_name": display_name,
        "message": content,
        "message_type": message_type,
        "platform": platform,
        "channel_id": channel_id,
        "server_id": server_id,
        "metadata": metadata or {},
    }
