"""
Platform-reserved commands for Waddles.

This module defines reserved commands for various streaming platforms
(Twitch, Discord, Slack, YouTube, and Kick) and provides utilities
to check if a command conflicts with platform-reserved commands.
"""

# Platform-reserved commands mapping
RESERVED_COMMANDS = {
    "twitch": {
        "/ban", "/unban", "/timeout", "/untimeout", "/slow", "/slowoff",
        "/followers", "/followersoff", "/subscribers", "/subscribersoff",
        "/clear", "/uniquechat", "/uniquechatoff", "/emoteonly", "/emoteonlyoff",
        "/commercial", "/host", "/unhost", "/raid", "/unraid", "/marker",
        "/mod", "/unmod", "/vip", "/unvip", "/block", "/unblock", "/color",
        "/disconnect", "/help", "/me", "/mods", "/vips", "/vote", "/w"
    },
    "discord": {
        "/ban", "/kick", "/timeout", "/mute", "/unmute", "/deafen", "/undeafen",
        "/move", "/nick", "/role", "/slowmode", "/purge", "/lock", "/unlock"
    },
    "slack": {
        "/archive", "/call", "/collapse", "/dnd", "/expand", "/feed", "/invite",
        "/invite_people", "/kick", "/leave", "/me", "/msg", "/mute", "/open",
        "/prefs", "/remind", "/remove", "/rename", "/search", "/shrug",
        "/shortcuts", "/star", "/status", "/topic", "/who"
    },
    "youtube": {
        "/ban", "/unban", "/slow", "/slowoff", "/membersonly", "/membersonlyoff"
    },
    "kick": {
        "/ban", "/unban", "/timeout", "/untimeout", "/slow", "/slowoff",
        "/clear", "/mod", "/unmod", "/vip", "/unvip"
    }
}


def is_reserved_command(command: str, platform: str = None) -> bool:
    """
    Check if a command is reserved on a specific platform or any platform.

    Args:
        command: The command to check (e.g., "/ban")
        platform: The platform to check against (e.g., "twitch", "discord").
                 If None, checks against all platforms.

    Returns:
        True if the command is reserved on the specified platform(s), False otherwise.

    Raises:
        ValueError: If the platform is not recognized.

    Examples:
        >>> is_reserved_command("/ban", "twitch")
        True
        >>> is_reserved_command("/custom", "twitch")
        False
        >>> is_reserved_command("/ban")
        True
        >>> is_reserved_command("/custom")
        False
    """
    if platform is not None:
        platform = platform.lower()
        if platform not in RESERVED_COMMANDS:
            raise ValueError(f"Unknown platform: {platform}")
        return command in RESERVED_COMMANDS[platform]

    # Check against all platforms
    for reserved_set in RESERVED_COMMANDS.values():
        if command in reserved_set:
            return True
    return False


def get_conflicting_platforms(command: str) -> list:
    """
    Get a list of platforms where a command is reserved.

    Args:
        command: The command to check (e.g., "/ban")

    Returns:
        A list of platform names where the command is reserved.
        Returns an empty list if the command is not reserved on any platform.

    Examples:
        >>> get_conflicting_platforms("/ban")
        ['twitch', 'discord', 'youtube', 'kick']
        >>> get_conflicting_platforms("/custom")
        []
        >>> get_conflicting_platforms("/me")
        ['twitch', 'discord', 'slack']
    """
    conflicting_platforms = []
    for platform, commands in RESERVED_COMMANDS.items():
        if command in commands:
            conflicting_platforms.append(platform)
    return conflicting_platforms
