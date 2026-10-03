"""Twitch OAuth2 HTTP primitives for the per-tenant bot-install flow (Bar Citizen, migration 0034).

Each tenant N brings its OWN Twitch application (`client_id`/`client_secret`,
registered by the tenant admin in the Twitch developer console) -- unlike
`services/oauth_providers.py`'s SaaS-wide `PROVIDERS` registry (env-var
client credentials, one app for the whole SaaS tier-0 tenant), every
function here takes the caller's own `client_id`/`client_secret` as
explicit arguments and never reads an env var for them. Tenant 0 keeps
using the SaaS Twitch app via `services.credential_resolver`; this module
is the tenant-N lane, driven by `blueprints/v1/tenant_twitch_install.py`.

Scopes requested are exactly what the Bar Citizen role-sync worker's Helix
calls need, no more:

- `channel:read:subscriptions` -- Get Broadcaster Subscriptions (subscriber tiers)
- `moderation:read` -- Get Moderators
- `channel:manage:moderators` -- Add/Remove Channel Moderator

Twitch rotates refresh tokens on every `grant_type=refresh_token` exchange
-- the previous refresh token is invalidated the instant a new one is
issued. `refresh_access_token()`'s caller (`services.
twitch_install_credentials`) relies on exactly this: a refresh call that
fails is always treated as the stored refresh token having already been
consumed (replay/compromise) rather than retried with the same value --
security.md Service-to-Service Auth's "a reused refresh token is treated
as compromise, revokes the whole chain" rule, enforced here by Twitch's
own token semantics rather than hub-api tracking token history itself.

Never logs a code/token/secret -- failures log HTTP status only, same
contract as `services/oauth_providers.py`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from services.errors import ApiError
from services.url_guard import validate_outbound_url

logger = logging.getLogger(__name__)

#: Connect within 5s, whole request within 15s -- matches `oauth_providers.py`.
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_ALLOWED_SCHEMES: tuple[str, ...] = ("https",)

TWITCH_AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"  # noqa: S105 - URL, not a credential

#: The exact Helix scopes the role-sync worker needs -- see module docstring.
ROLE_SYNC_SCOPES: tuple[str, ...] = (
    "channel:read:subscriptions",
    "moderation:read",
    "channel:manage:moderators",
)


class TwitchOAuthError(RuntimeError):
    """Non-2xx/malformed response from Twitch's token endpoint, or a transport failure.

    Message is always safe to show/log -- HTTP status only, never a token,
    code, or secret.
    """


@dataclass(slots=True, frozen=True)
class TwitchTokenResult:
    """Normalized Twitch token-exchange/refresh result."""

    access_token: str
    refresh_token: str
    expires_in: int | None
    scopes: list[str]
    token_type: str


def build_authorize_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    """Build Twitch's authorize-redirect URL requesting `ROLE_SYNC_SCOPES`.

    `force_verify=true` always re-prompts the bot account for consent --
    deliberate: this installs a bot account's authorization into a
    tenant-supplied app, not a user login, so a silently-reused prior
    grant on the same Twitch account would be a trap for an admin who
    intended to re-authorize with a different account.
    """
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(ROLE_SYNC_SCOPES),
        "state": state,
        "force_verify": "true",
    }
    return f"{TWITCH_AUTHORIZE_URL}?{urlencode(params)}"


async def _guard_url(url: str) -> None:
    """Re-validate `url` through the shared SSRF guard before any outbound request."""
    try:
        await validate_outbound_url(url, allowed_schemes=_ALLOWED_SCHEMES)
    except ApiError as exc:
        raise TwitchOAuthError(f"outbound URL blocked: {exc.message}") from exc


async def _post_token(data: dict[str, str]) -> dict[str, Any]:
    """POST `data` to Twitch's token endpoint, returning the decoded JSON object.

    Raises `TwitchOAuthError` on transport failure, a non-2xx response, or
    a response body that isn't a JSON object. Never logs `data` (it always
    carries `client_secret` and/or a code/refresh token).
    """
    await _guard_url(TWITCH_TOKEN_URL)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                TWITCH_TOKEN_URL, data=data, headers={"Accept": "application/json"}
            )
    except httpx.HTTPError as exc:
        logger.warning("twitch_install_oauth.token_request_failed")
        raise TwitchOAuthError("twitch: token request failed") from exc

    if response.status_code // 100 != 2:
        logger.warning("twitch_install_oauth.token_request_non_2xx status=%d", response.status_code)
        raise TwitchOAuthError(f"twitch: token endpoint returned HTTP {response.status_code}")

    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise TwitchOAuthError("twitch: token endpoint returned malformed JSON") from exc

    if not isinstance(payload, dict):
        raise TwitchOAuthError("twitch: token endpoint returned malformed JSON")

    return payload


def _result_from_payload(payload: dict[str, Any]) -> TwitchTokenResult:
    """Map Twitch's raw token JSON object into a normalized `TwitchTokenResult`.

    Both `access_token` and `refresh_token` are required here (unlike
    `oauth_providers.TokenResponse`, where `refresh_token` is optional) --
    the role-sync worker cannot function without a refresh token to renew
    against, so a Twitch response missing one fails the install/refresh
    outright rather than silently degrading to a token that expires with
    no way to renew it.
    """
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise TwitchOAuthError("twitch: token endpoint response missing access_token")

    refresh_token = payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise TwitchOAuthError("twitch: token endpoint response missing refresh_token")

    expires_in_raw = payload.get("expires_in")
    expires_in = int(expires_in_raw) if isinstance(expires_in_raw, int | float) else None

    scope_raw = payload.get("scope")
    if isinstance(scope_raw, list):
        scopes = [str(item) for item in scope_raw]
    elif isinstance(scope_raw, str):
        scopes = scope_raw.split()
    else:
        scopes = list(ROLE_SYNC_SCOPES)

    token_type = str(payload.get("token_type") or "bearer")

    return TwitchTokenResult(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        scopes=scopes,
        token_type=token_type,
    )


async def exchange_code(
    *, client_id: str, client_secret: str, code: str, redirect_uri: str
) -> TwitchTokenResult:
    """Exchange an authorization `code` for a `TwitchTokenResult` using the tenant's own app creds.

    Raises `TwitchOAuthError` on any transport/response failure. Never
    logs `client_secret` or `code`.
    """
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    payload = await _post_token(data)
    return _result_from_payload(payload)


async def refresh_access_token(
    *, client_id: str, client_secret: str, refresh_token: str
) -> TwitchTokenResult:
    """Refresh an access token using the tenant's own app creds.

    Twitch issues a brand-new `refresh_token` on every successful call and
    immediately invalidates the one just spent -- callers MUST persist the
    returned `TwitchTokenResult.refresh_token`, never the one passed in,
    and MUST treat any failure here as the given `refresh_token` no longer
    being valid (already rotated, revoked, or replayed) -- see module
    docstring. Never logs `client_secret` or either refresh token value.
    """
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    payload = await _post_token(data)
    return _result_from_payload(payload)
