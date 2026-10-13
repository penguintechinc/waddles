"""Linked-identity management -- ported from `identityController.js`.

Scoped to the unified-auth model `authController.js` now uses:
`req.user.userId` (this port: the JWT `sub` claim, via
`services.current_user.get_current_user_id`) is already `hub_users.id`.
Node's `identityController.js` still carries a `getOrCreateHubUser()`
helper for a pre-unified-auth, platform-session model (`user.platform ===
'admin'` / bare platform-identity sessions with no `hub_users` row yet)
-- dead code under the current `authController.js` login flow, which
always creates/resolves a `hub_users` row before minting a token. Not
ported: every route here operates directly on the caller's
`hub_users.id`, matching how `authController.js` itself already treats
`req.user.userId`.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from services import ban_suppression
from services.errors import bad_request, not_found

VALID_LINK_PLATFORMS = ("discord", "twitch", "slack")
_LINK_STATE_TTL = timedelta(minutes=10)


async def list_identities(async_dal: Any, dal: Any, *, user_id: int) -> list[Any]:
    """List identities."""
    rows = await async_dal.select_async(
        dal(dal.hub_user_identities.hub_user_id == user_id),
        orderby=(~dal.hub_user_identities.is_primary, ~dal.hub_user_identities.linked_at),
    )
    return list(rows)


async def get_primary_identity(async_dal: Any, dal: Any, *, user_id: int) -> Any | None:
    """Get primary identity."""
    rows = await async_dal.select_async(
        dal(
            (dal.hub_user_identities.hub_user_id == user_id)
            & (dal.hub_user_identities.is_primary == True)  # noqa: E712 - pydal Field comparison
        )
    )
    return rows.first() if rows else None


async def set_primary_identity(async_dal: Any, dal: Any, *, user_id: int, platform: str) -> None:
    """Set primary identity."""
    if not platform:
        raise bad_request("Platform required")

    exists = await async_dal.select_async(
        dal(
            (dal.hub_user_identities.hub_user_id == user_id)
            & (dal.hub_user_identities.platform == platform)
        )
    )
    if not exists:
        raise not_found("Identity not found")

    await async_dal.update_async(dal.hub_user_identities.hub_user_id == user_id, is_primary=False)
    await async_dal.update_async(
        (dal.hub_user_identities.hub_user_id == user_id)
        & (dal.hub_user_identities.platform == platform),
        is_primary=True,
    )


async def unlink_identity(async_dal: Any, dal: Any, *, user_id: int, platform: str) -> None:
    """Unlink identity."""
    count = await async_dal.count_async(dal.hub_user_identities.hub_user_id == user_id)
    if count <= 1:
        raise bad_request("Cannot unlink last identity")

    rows = await async_dal.select_async(
        dal(
            (dal.hub_user_identities.hub_user_id == user_id)
            & (dal.hub_user_identities.platform == platform)
        )
    )
    if not rows:
        raise not_found("Identity not found")
    was_primary = bool(rows.first().is_primary)

    await async_dal.delete_async(
        (dal.hub_user_identities.hub_user_id == user_id)
        & (dal.hub_user_identities.platform == platform)
    )

    if was_primary:
        remaining = await async_dal.select_async(
            dal(dal.hub_user_identities.hub_user_id == user_id),
            orderby=dal.hub_user_identities.linked_at,
            limitby=(0, 1),
        )
        if remaining:
            await async_dal.update_async(
                dal.hub_user_identities.id == remaining.first().id, is_primary=True
            )


# ---------------------------------------------------------------------------
# Identity-linking OAuth flow -- SECURITY: state MUST be resolved from
# server-side storage, never decoded from a client-suppliable value. The
# very first port of this function encoded state as a self-contained,
# UNSIGNED base64 JSON blob carrying `hubUserId` directly -- forgeable by
# any caller (craft your own base64 blob with a victim's hubUserId, run
# your own OAuth flow, and identity_link_callback would link YOUR OAuth
# identity to the VICTIM's hub_users row; since OAuth login resolves by
# (platform, platform_user_id), that identity then logs you into their
# account -- full account takeover). Fixed by switching to the exact same
# `hub_oauth_states` table `oauth_service.start_link()`/`link_callback()`
# already use (server-side `user_id` column, single-use via DELETE on
# consume, TTL via `expires_at`) -- see `hub_api/PORTING.md` for the
# writeup. Kept as its own function pair (not a call to oauth_service's
# versions) only because the redirect_uri path differs
# (`/api/v1/user/identities/link/:platform/callback`, the frozen v1
# contract path, vs oauth_service's own `/api/v1/auth/oauth/:platform/
# link-callback`) -- the state mechanism itself is now identical.
# ---------------------------------------------------------------------------


async def start_identity_link(
    async_dal: Any, dal: Any, *, user_id: int, platform: str, callback_base_url: str
) -> tuple[str, str]:
    """Start an identity-link OAuth flow. Returns `(authorize_url, state)`.

    `state` is an opaque, unguessable token (`secrets.token_hex(16)`) --
    the actual `user_id` binding lives server-side in `hub_oauth_states`,
    never in the token itself.
    """
    from services import oauth_service  # local import: avoids a module-load cycle

    exists = await async_dal.select_async(
        dal(
            (dal.hub_user_identities.hub_user_id == user_id)
            & (dal.hub_user_identities.platform == platform)
        )
    )
    if exists:
        raise bad_request(f"{platform} account already linked")

    state = secrets.token_hex(16)
    await async_dal.insert_async(
        dal.hub_oauth_states,
        state=state,
        mode="link",
        platform=platform,
        user_id=user_id,
        expires_at=datetime.now(UTC) + _LINK_STATE_TTL,
        created_at=datetime.now(UTC),
    )

    redirect_uri = f"{callback_base_url}/api/v1/user/identities/link/{platform}/callback"
    authorize_url = await oauth_service.build_authorize_url(
        async_dal, dal, platform=platform, redirect_uri=redirect_uri, state=state
    )
    return authorize_url, state


async def identity_link_callback(
    async_dal: Any, dal: Any, *, platform: str, code: str, state: str, callback_base_url: str
) -> None:
    """Complete an identity-link callback: resolve state server-side, exchange the code, link.

    `state` is looked up in `hub_oauth_states` (never decoded/trusted from
    the caller) and consumed (deleted) immediately -- single-use, and a
    replay after consumption or after `expires_at` fails the lookup and
    raises `bad_request`, same as an invalid state.
    """
    from services import oauth_service  # local import: avoids a module-load cycle

    state_rows = await async_dal.select_async(
        dal(
            (dal.hub_oauth_states.state == state)
            & (dal.hub_oauth_states.platform == platform)
            & (dal.hub_oauth_states.mode == "link")
            & (dal.hub_oauth_states.expires_at > datetime.now(UTC))
        )
    )
    if not state_rows:
        raise bad_request("Invalid or expired state")
    hub_user_id = state_rows.first().user_id
    await async_dal.delete_async(dal.hub_oauth_states.state == state)

    redirect_uri = f"{callback_base_url}/api/v1/user/identities/link/{platform}/callback"
    user_data = await oauth_service.exchange_code(
        async_dal, dal, platform=platform, code=code, redirect_uri=redirect_uri
    )

    existing = await async_dal.select_async(
        dal(
            (dal.hub_user_identities.platform == platform)
            & (dal.hub_user_identities.platform_user_id == user_data["id"])
        )
    )
    if existing:
        if existing.first().hub_user_id != hub_user_id:
            raise bad_request("Already linked to another user")
        # Already linked -- re-run the suppression re-check in case a previous attempt inserted the
        # link and then failed inside it (e.g. key outage), so a retry never leaves it un-applied.
        await ban_suppression.reapply_for_hub_user_async(async_dal, dal, hub_user_id=hub_user_id)
        return

    count = await async_dal.count_async(dal.hub_user_identities.hub_user_id == hub_user_id)
    await async_dal.insert_async(
        dal.hub_user_identities,
        hub_user_id=hub_user_id,
        platform=platform,
        platform_user_id=user_data["id"],
        platform_username=user_data["username"],
        avatar_url=user_data.get("avatar_url"),
        is_primary=(count == 0),
        linked_at=datetime.now(UTC),
        last_used=datetime.now(UTC),
    )
    # Ban-evasion suppression: a banned person may join a community first and link the banned
    # platform account afterwards -- re-apply any matching restriction to this user's
    # memberships now that the platform id is known (see `services/ban_suppression.py`).
    await ban_suppression.reapply_for_hub_user_async(async_dal, dal, hub_user_id=hub_user_id)
