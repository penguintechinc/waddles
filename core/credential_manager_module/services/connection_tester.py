"""Validate a stored operator BOT token against its provider's own API (gh-969).

The superadmin "test connection" check needs the DECRYPTED operator bot token. hub-api's DB role
is denied `platform_integrations` (H-3) and has no function returning bot secrets, so the check
lives here -- the credential manager is the sanctioned operator-secret holder. The token is
decrypted in memory for one provider call and never returned, logged, or persisted.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

TWITCH_VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
DISCORD_ME_URL = "https://discord.com/api/users/@me"
SLACK_AUTH_TEST_URL = "https://slack.com/api/auth.test"
YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels?part=id&mine=true"

#: platform -> (validation URL, Authorization scheme). Slack is a POST and handled separately.
_GET_TESTERS: dict[str, tuple[str, str]] = {
    "twitch": (TWITCH_VALIDATE_URL, "OAuth"),
    "discord": (DISCORD_ME_URL, "Bearer"),
    "youtube": (YOUTUBE_CHANNELS_URL, "Bearer"),
}


async def validate_token(
    http: httpx.AsyncClient, platform: str, access_token: str
) -> tuple[bool, str | None]:
    """Return `(valid, error)`; fails closed on any transport error. Unknown platform = valid."""
    try:
        if platform == "slack":
            resp = await http.post(
                SLACK_AUTH_TEST_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
            ok = bool(resp.json().get("ok"))
            return ok, None if ok else "invalid token"
        if platform in _GET_TESTERS:
            url, scheme = _GET_TESTERS[platform]
            resp = await http.get(url, headers={"Authorization": f"{scheme} {access_token}"})
            return resp.is_success, None if resp.is_success else f"HTTP {resp.status_code}"
        return True, None
    except httpx.HTTPError as exc:
        logger.warning(
            "connection test transport failure platform=%s type=%s", platform, type(exc).__name__
        )
        return False, type(exc).__name__
