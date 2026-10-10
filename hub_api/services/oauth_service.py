"""OAuth login/linking -- ported from `authController.js`'s OAuth functions.

Scoped to the three platforms `flask_core.auth.OAUTH_PROVIDERS` already
knows about (Discord, Twitch, Slack) -- Node's authController also
supports `youtube`/`kick`, which flask_core's OAuth config table doesn't
model yet. Deliberate scope reduction, not a silent gap: tracked in
`hub_api/PORTING.md` as a follow-up for whichever group needs those two
platforms next; `VALID_PLATFORMS` below is the single place that list
grows.

Node's `authController.js` tries an "Identity Core" service first
(`config.identity.apiUrl`) and falls back to exchanging the code directly
with the platform. No such Identity Core service is wired into hub-api's
own config (`HubAPIConfig` has no `identity_api_url`), so this port goes
straight to the direct-exchange path Node itself falls back to --
functionally the same end state, one fewer network hop, no Identity
Core dependency introduced.
"""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from config import HubAPIConfig
from services.auth_service import SessionUser, create_session_token
from services.errors import bad_request, conflict

VALID_PLATFORMS = ("discord", "twitch", "slack")

#: TTL for the opaque OAuth exchange code minted by `oauth_callback` --
#: long enough for the frontend's own redirect+fetch round trip, short
#: enough that a leaked code (e.g. still visible in a browser history entry
#: for the split-second before it's consumed) is worthless shortly after.
EXCHANGE_CODE_TTL_SECONDS = 60

_AUTHORIZE_URLS = {
    "discord": "https://discord.com/api/oauth2/authorize",
    "twitch": "https://id.twitch.tv/oauth2/authorize",
    "slack": "https://slack.com/oauth/v2/authorize",
}
_TOKEN_URLS = {
    "discord": "https://discord.com/api/oauth2/token",
    "twitch": "https://id.twitch.tv/oauth2/token",
    "slack": "https://slack.com/api/oauth.v2.access",
}
_SCOPES = {
    "discord": "identify email",
    "twitch": "user:read:email",
    "slack": "identity.basic,identity.email,identity.avatar",
}


async def _get_platform_credentials(async_dal: Any, dal: Any, platform: str) -> dict[str, str]:
    """`platform_configs` row, falling back to `{PLATFORM}_CLIENT_ID`/`_SECRET` env vars."""
    # pydal query builder, not raw SQL -- see hub_api/PORTING.md Gotcha #1
    # (async_dal's raw-SQL helpers hardcode %s/psycopg2-only placeholders).
    rows = await async_dal.select_async(dal(dal.platform_configs.platform == platform))
    creds = {row.config_key: row.config_value for row in rows}
    prefix = platform.upper()
    if not creds.get("client_id"):
        creds["client_id"] = os.getenv(f"{prefix}_CLIENT_ID", "")
    if not creds.get("client_secret"):
        creds["client_secret"] = os.getenv(f"{prefix}_CLIENT_SECRET", "")
    return creds


def _require_valid_platform(platform: str) -> None:
    if platform not in VALID_PLATFORMS:
        raise bad_request("Invalid platform")


def _generate_authorize_url(platform: str, client_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": _SCOPES[platform],
        "state": state,
    }
    return f"{_AUTHORIZE_URLS[platform]}?{urlencode(params)}"


async def build_authorize_url(
    async_dal: Any, dal: Any, *, platform: str, redirect_uri: str, state: str
) -> str:
    """Public wrapper used by `identity_service.start_identity_link` (its own state scheme)."""
    _require_valid_platform(platform)
    creds = await _get_platform_credentials(async_dal, dal, platform)
    if not creds["client_id"]:
        raise bad_request(f"No OAuth credentials configured for {platform}")
    return _generate_authorize_url(platform, creds["client_id"], redirect_uri, state)


async def exchange_code(
    async_dal: Any, dal: Any, *, platform: str, code: str, redirect_uri: str
) -> dict[str, Any]:
    """Public wrapper around `_exchange_code` for callers outside this module."""
    _require_valid_platform(platform)
    creds = await _get_platform_credentials(async_dal, dal, platform)
    return await _exchange_code(platform, creds, code, redirect_uri)


async def start_oauth(
    async_dal: Any, dal: Any, *, platform: str, mode: str, callback_base_url: str
) -> tuple[str, str]:
    """Start an OAuth login/link flow. Returns `(authorize_url, state)`.

    `hub_oauth_states` has no `metadata` column (see `schema.py`'s module
    docstring gap 2) -- the tenant-scoped `metadata` insert Node performs
    for login-mode state is not reproduced; OAuth login resolves against
    `DEFAULT_TENANT_SLUG` only, matching this deployment's actual
    default-tenant-only reality rather than a call that would 500 against
    the real schema.
    """
    _require_valid_platform(platform)
    state = secrets.token_hex(16)
    redirect_uri = f"{callback_base_url}/api/v1/auth/oauth/{platform}/callback"

    await async_dal.insert_async(
        dal.hub_oauth_states,
        state=state,
        mode=mode or "login",
        platform=platform,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        created_at=datetime.now(UTC),
    )

    creds = await _get_platform_credentials(async_dal, dal, platform)
    if not creds["client_id"]:
        raise bad_request(f"No OAuth credentials configured for {platform}")
    return _generate_authorize_url(platform, creds["client_id"], redirect_uri, state), state


async def _exchange_code(
    platform: str, creds: dict[str, str], code: str, redirect_uri: str
) -> dict[str, Any]:
    """Exchange an authorization code for platform user info. Discord/Twitch/Slack only."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        if platform == "discord":
            token_resp = await client.post(
                _TOKEN_URLS["discord"],
                content=urlencode(
                    {
                        "client_id": creds["client_id"],
                        "client_secret": creds["client_secret"],
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                    }
                ),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            token_resp.raise_for_status()
            access_token = token_resp.json()["access_token"]
            user_resp = await client.get(
                "https://discord.com/api/users/@me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            user_resp.raise_for_status()
            u = user_resp.json()
            avatar = (
                f"https://cdn.discordapp.com/avatars/{u['id']}/{u['avatar']}.png"
                if u.get("avatar")
                else None
            )
            return {
                "id": u["id"],
                "username": u["username"],
                "email": u.get("email"),
                "avatar_url": avatar,
            }

        if platform == "twitch":
            token_resp = await client.post(
                _TOKEN_URLS["twitch"],
                content=urlencode(
                    {
                        "client_id": creds["client_id"],
                        "client_secret": creds["client_secret"],
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                    }
                ),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            token_resp.raise_for_status()
            access_token = token_resp.json()["access_token"]
            user_resp = await client.get(
                "https://api.twitch.tv/helix/users",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Client-Id": creds["client_id"],
                },
            )
            user_resp.raise_for_status()
            u = user_resp.json()["data"][0]
            return {
                "id": u["id"],
                "username": u["login"],
                "email": u.get("email"),
                "avatar_url": u.get("profile_image_url"),
            }

        if platform == "slack":
            token_resp = await client.post(
                _TOKEN_URLS["slack"],
                content=urlencode(
                    {
                        "client_id": creds["client_id"],
                        "client_secret": creds["client_secret"],
                        "code": code,
                        "redirect_uri": redirect_uri,
                    }
                ),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            token_resp.raise_for_status()
            data = token_resp.json()
            if not data.get("ok"):
                raise bad_request(f"Slack OAuth error: {data.get('error')}")
            identity_resp = await client.get(
                "https://slack.com/api/users.identity",
                headers={"Authorization": f"Bearer {data['authed_user']['access_token']}"},
            )
            identity_resp.raise_for_status()
            identity = identity_resp.json()["user"]
            return {
                "id": identity["id"],
                "username": identity["name"],
                "email": identity.get("email"),
                "avatar_url": identity.get("image_192") or identity.get("image_72"),
            }

        raise bad_request(f"OAuth exchange not implemented for {platform}")


async def _find_or_create_user_from_oauth(
    async_dal: Any, dal: Any, *, platform: str, user_data: dict[str, Any]
) -> Any:
    """Resolve the hub user for an OAuth login. Never adopts an existing row by email.

    SECURITY: only ever resolves an existing user via an EXISTING, already-
    linked `(platform, platform_user_id)` row -- never by matching the
    OAuth provider's claimed email against `hub_users.email`. The
    original port of this function did the latter (faithfully matching
    Node's `authController.js::findOrCreateUserFromOAuth`, which has the
    identical bug): an attacker who registers an OAuth account using a
    victim's email (providers vary in whether that email is verified, and
    some don't verify it at all) would be silently logged in AS the
    victim's existing hub account. Fixed: an email match with no existing
    identity link raises `conflict()` instead of merging -- intentional
    linking only happens through the AUTHENTICATED `/oauth/<platform>/
    link` flow (`start_link`/`link_callback` below), where the caller has
    already proven who they are via their own session. See
    `hub_api/PORTING.md` for the writeup; this is a real, exploitable gap
    in the current Node source, not just a hypothetical one.
    """
    identity_rows = await async_dal.select_async(
        dal(
            (dal.hub_user_identities.platform == platform)
            & (dal.hub_user_identities.platform_user_id == user_data["id"])
        ),
        dal.hub_users.ALL,
        left=dal.hub_users.on(dal.hub_users.id == dal.hub_user_identities.hub_user_id),
    )
    if identity_rows:
        await async_dal.update_async(
            (dal.hub_user_identities.platform == platform)
            & (dal.hub_user_identities.platform_user_id == user_data["id"]),
            platform_username=user_data["username"],
            avatar_url=user_data.get("avatar_url"),
            last_used=datetime.now(UTC),
        )
        # A single table's `.ALL` selected against a LEFT JOIN condition
        # returns a flat Row (fields accessed directly), not nested under
        # `.hub_users` -- nesting only happens when >1 table's fields are
        # requested together (confirmed empirically; see PORTING.md).
        u = identity_rows[0]
        return SessionUser(
            id=u.id,
            email=u.email,
            username=u.username,
            avatar_url=u.avatar_url,
            is_super_admin=bool(u.is_super_admin),
            is_vendor=bool(u.is_vendor),
            is_analytics_consumer=bool(u.is_analytics_consumer),
        )

    email = user_data.get("email") or f"{user_data['username']}@{platform}.local"
    existing = await async_dal.select_async(dal(dal.hub_users.email == email.lower()))
    if existing:
        raise conflict(
            "An account with this email already exists. Log in and link this "
            "platform from your account settings instead."
        )

    new_id = await async_dal.insert_async(
        dal.hub_users,
        email=email.lower(),
        username=user_data["username"],
        avatar_url=user_data.get("avatar_url"),
        is_active=True,
        created_at=datetime.now(UTC),
    )
    user = SessionUser(
        id=new_id,
        email=email.lower(),
        username=user_data["username"],
        avatar_url=user_data.get("avatar_url"),
    )
    from services.auth_service import add_user_to_global_community

    await add_user_to_global_community(async_dal, dal, user_id=new_id)

    await async_dal.insert_async(
        dal.hub_user_identities,
        hub_user_id=user.id,
        platform=platform,
        platform_user_id=user_data["id"],
        platform_username=user_data["username"],
        avatar_url=user_data.get("avatar_url"),
        linked_at=datetime.now(UTC),
    )
    return user


async def oauth_callback(
    async_dal: Any,
    dal: Any,
    cfg: HubAPIConfig,
    *,
    platform: str,
    code: str,
    state: str,
    callback_base_url: str,
) -> str:
    """Complete an OAuth login. Returns a minted session JWT."""
    state_rows = await async_dal.select_async(
        dal(
            (dal.hub_oauth_states.state == state)
            & (dal.hub_oauth_states.platform == platform)
            & (dal.hub_oauth_states.expires_at > datetime.now(UTC))
        )
    )
    if not state_rows:
        raise bad_request("Invalid or expired OAuth state")
    await async_dal.delete_async(dal.hub_oauth_states.state == state)

    redirect_uri = f"{callback_base_url}/api/v1/auth/oauth/{platform}/callback"
    creds = await _get_platform_credentials(async_dal, dal, platform)
    user_data = await _exchange_code(platform, creds, code, redirect_uri)
    user = await _find_or_create_user_from_oauth(
        async_dal, dal, platform=platform, user_data=user_data
    )

    return await create_session_token(
        async_dal, dal, cfg, user=user, auth_method=f"oauth_{platform}"
    )


async def create_oauth_exchange_code(
    async_dal: Any, dal: Any, *, token: str, platform: str
) -> str:
    """Mint a short-lived, single-use opaque code standing in for a just-issued session JWT.

    Used by `blueprints/v1/auth.py::oauth_callback` to hand the session off
    to the frontend via the OAuth redirect WITHOUT putting the JWT itself in
    the URL -- query strings leak into proxy/access logs, browser history,
    and the `Referer` header of any outbound request the callback page
    happens to make. The code is redeemed exactly once, server-side, via
    `redeem_oauth_exchange_code` (`POST /api/v1/auth/exchange`).
    """
    code = secrets.token_urlsafe(32)
    await async_dal.insert_async(
        dal.hub_oauth_exchange_codes,
        code=code,
        token=token,
        platform=platform,
        used=False,
        expires_at=datetime.now(UTC) + timedelta(seconds=EXCHANGE_CODE_TTL_SECONDS),
        created_at=datetime.now(UTC),
    )
    return code


async def redeem_oauth_exchange_code(async_dal: Any, dal: Any, *, code: str) -> str:
    """Redeem a single-use OAuth exchange code for the session JWT it stands in for.

    The `UPDATE ... WHERE used = FALSE AND expires_at > NOW()` clause is the
    atomic claim -- the database (not a separate app-level lock) decides
    which concurrent caller, if any, wins a race against the same code,
    mirroring `community_welcomed_users`'s `ON CONFLICT DO NOTHING` pattern
    (migration 068). A second redemption attempt against the same code
    updates zero rows and is rejected, same as an unknown or expired one.
    """
    claimed = await async_dal.update_async(
        (dal.hub_oauth_exchange_codes.code == code)
        & (dal.hub_oauth_exchange_codes.used == False)  # noqa: E712 - pydal Field comparison
        & (dal.hub_oauth_exchange_codes.expires_at > datetime.now(UTC)),
        used=True,
        used_at=datetime.now(UTC),
    )
    if not claimed:
        raise bad_request("Invalid or expired exchange code")

    rows = await async_dal.select_async(dal(dal.hub_oauth_exchange_codes.code == code))
    return str(rows.first().token)


async def start_link(
    async_dal: Any, dal: Any, *, platform: str, user_id: int, callback_base_url: str
) -> tuple[str, str]:
    """Start linking `platform` to the currently-authenticated user."""
    _require_valid_platform(platform)
    state = secrets.token_hex(16)
    redirect_uri = f"{callback_base_url}/api/v1/auth/oauth/{platform}/link-callback"

    await async_dal.insert_async(
        dal.hub_oauth_states,
        state=state,
        mode="link",
        platform=platform,
        user_id=user_id,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        created_at=datetime.now(UTC),
    )

    creds = await _get_platform_credentials(async_dal, dal, platform)
    if not creds["client_id"]:
        raise bad_request(f"No OAuth credentials configured for {platform}")
    return _generate_authorize_url(platform, creds["client_id"], redirect_uri, state), state


async def link_callback(
    async_dal: Any, dal: Any, *, platform: str, code: str, state: str, callback_base_url: str
) -> None:
    """Complete an OAuth account-link flow for an already-authenticated user."""
    state_rows = await async_dal.select_async(
        dal(
            (dal.hub_oauth_states.state == state)
            & (dal.hub_oauth_states.platform == platform)
            & (dal.hub_oauth_states.mode == "link")
            & (dal.hub_oauth_states.expires_at > datetime.now(UTC))
        )
    )
    if not state_rows:
        raise bad_request("Invalid or expired OAuth state")
    user_id = state_rows.first().user_id
    await async_dal.delete_async(dal.hub_oauth_states.state == state)

    redirect_uri = f"{callback_base_url}/api/v1/auth/oauth/{platform}/link-callback"
    creds = await _get_platform_credentials(async_dal, dal, platform)
    user_data = await _exchange_code(platform, creds, code, redirect_uri)

    existing = await async_dal.select_async(
        dal(
            (dal.hub_user_identities.platform == platform)
            & (dal.hub_user_identities.platform_user_id == user_data["id"])
        )
    )
    if existing and existing.first().hub_user_id != user_id:
        raise bad_request("Platform account already linked to another user")
    if existing:
        return  # already linked to this same user

    await async_dal.insert_async(
        dal.hub_user_identities,
        hub_user_id=user_id,
        platform=platform,
        platform_user_id=user_data["id"],
        platform_username=user_data["username"],
        avatar_url=user_data.get("avatar_url"),
        linked_at=datetime.now(UTC),
        last_used=datetime.now(UTC),
    )


async def unlink_account(async_dal: Any, dal: Any, *, user_id: int, platform: str) -> None:
    """Unlink account."""
    user_rows = await async_dal.select_async(dal(dal.hub_users.id == user_id))
    has_password = bool(user_rows) and user_rows.first().password_hash

    if not has_password:
        count = await async_dal.count_async(dal(dal.hub_user_identities.hub_user_id == user_id))
        if count <= 1:
            raise bad_request(
                "Cannot unlink last platform without a password. Please set a password first."
            )

    await async_dal.delete_async(
        (dal.hub_user_identities.hub_user_id == user_id)
        & (dal.hub_user_identities.platform == platform)
    )
