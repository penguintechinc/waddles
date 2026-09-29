"""`DiscordGuildAuthorityVerifier` -- Discord impl of the pairing service's authority check.

`feature/guild-pairing-api` (concurrent branch, #500 pairing/binding/role
REST) defines a `GuildAuthorityVerifier` protocol for confirming a user
claiming guild-authority actually holds it, used wherever that slice needs
a "does this caller control this guild" check independent of this OAuth
module's own install flow (contract doc Sec2's per-tenant bot-install
consent is the SOURCE of an active pairing; this verifier is a read-only
authority check against Discord for flows that need one without going
through a full install, e.g. re-verifying an existing pairing is still
backed by a guild admin).

Implemented here (own module, own branch) rather than on
`feature/guild-pairing-api` per the task split -- that branch wires this
class into its `GuildAuthorityVerifier`-typed call sites once merged.
Expected protocol shape (documented here since the protocol itself lives
on the other branch): an async method taking a Discord user access token
and a guild id, returning whether that user currently holds Discord's own
`MANAGE_GUILD` (Manage Server) permission bit in that guild -- this class
implements exactly that shape as `has_guild_authority()`, so wiring it in
is a straight `verifier = DiscordGuildAuthorityVerifier()` construction
once the protocol is merged, no adapter needed.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

#: Discord's own `MANAGE_GUILD` permission bit (Permissions bitfield, Discord API v10).
_MANAGE_GUILD_BIT = 0x00000020

_USER_GUILDS_URL = "https://discord.com/api/users/@me/guilds"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class DiscordGuildAuthorityError(RuntimeError):
    """Raised when Discord's API can't be reached or returns something unusable.

    Callers should treat this as "authority could not be confirmed" (fail
    closed to "no authority"), never as an implicit grant.
    """


class DiscordGuildAuthorityVerifier:
    """Confirms Discord `MANAGE_GUILD` (Manage Server) via the user's own OAuth token.

    Calls Discord's `GET /users/@me/guilds` with the caller-supplied
    Discord OAuth access token (`identify guilds` scope) and checks the
    target guild's `permissions` field for the `MANAGE_GUILD` bit --
    Discord returns each guild the token's user is a member of together
    with that user's own permission bitfield in it, so no second
    per-guild call is needed. Never uses a bot token for this check: the
    whole point is confirming the HUMAN, via their own consent-granted
    token, actually controls the guild -- a bot's own permissions are a
    different, unrelated question.
    """

    async def has_guild_authority(self, *, user_access_token: str, guild_id: str) -> bool:
        """Return whether `user_access_token`'s holder has Manage Server in `guild_id`.

        Fails closed (`False`) for a missing/expired token (401) or a
        guild the user isn't even a member of (not present in the list).
        Raises `DiscordGuildAuthorityError` only for a genuine transport
        failure or malformed response -- distinct from "confirmed no
        authority" so a caller can distinguish "denied" from "couldn't
        check right now" if it wants to (e.g. retry vs. hard-deny).
        """
        if not user_access_token or not guild_id:
            return False

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            try:
                response = await client.get(
                    _USER_GUILDS_URL,
                    headers={"Authorization": f"Bearer {user_access_token}"},
                )
            except httpx.HTTPError as exc:
                raise DiscordGuildAuthorityError(
                    "discord guild list request failed: network error"
                ) from exc

        if response.status_code == 401:
            return False
        if response.status_code >= 400:
            raise DiscordGuildAuthorityError(
                f"discord guild list request failed: HTTP {response.status_code}"
            )

        try:
            guilds = response.json()
        except ValueError as exc:
            raise DiscordGuildAuthorityError(
                "discord guild list response was malformed JSON"
            ) from exc
        if not isinstance(guilds, list):
            raise DiscordGuildAuthorityError("discord guild list response was not a list")

        for guild in guilds:
            if not isinstance(guild, dict) or str(guild.get("id")) != guild_id:
                continue
            try:
                permissions = int(guild.get("permissions", 0))
            except (TypeError, ValueError):
                return False
            return bool(permissions & _MANAGE_GUILD_BIT)

        return False
