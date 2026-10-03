"""Per-tenant Discord OAuth2 bot-install flow (Bar Citizen Units C/D), built on `#563`'s seam.

Every tenant beyond the global/SaaS tenant brings its OWN Discord
application (client id/secret/bot token) -- the server owner invites that
tenant's bot into their guild once, via this flow. Tenant 0 uses the SaaS
Discord credentials `services.credential_resolver.DefaultCredentialResolver`
already resolves from env vars; it never reaches this module (rejected
before a `state` is even minted, mirroring migration 0034's own
`trg_reject_global_tenant_credentials` DB trigger -- see
`services.credential_resolver.store_tenant_credentials`'s docstring for
the same "fail fast before the DB trigger" rationale this module reuses).

Flow:
1. `build_authorize_url()` -- a tenant admin submits their OWN newly
   created Discord application's `application_id`/`client_secret`
   (optionally `bot_token`). Nothing is persisted yet: the submitted
   credentials are stashed (encrypted) against a single-use `state` token
   (`services.tenant_discord_install_state`) and only survive the
   TTL'd round trip through Discord's consent screen.
2. Discord redirects the admin's browser back to the public callback with
   `code` + the `state` we minted.
3. `complete_install()` -- verifies+consumes `state` (forged/expired/
   replayed all rejected by `verify_and_consume_state`'s uniform `None`
   return), exchanges `code` for a token using the ADMIN'S OWN submitted
   client credentials (proves the code was genuinely issued to that
   application, not merely typed in), and only on a successful exchange
   calls `services.credential_resolver.store_tenant_credentials()` to
   persist the now-verified credentials. A failed exchange persists
   nothing -- fail closed.

Token exchange goes through `httpx`, matching `services/oauth_providers.py`'s
own HTTP-client choice, timeout budget, and `services/url_guard.py`
SSRF-guard-before-every-outbound-call posture (defense-in-depth: Discord's
token URL is a fixed constant here, not user input, but applied uniformly
per that module's own documented contract).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from services.credential_resolver import store_tenant_credentials
from services.errors import ApiError, bad_request
from services.tenant_discord_install_state import consume_state, create_state
from services.url_guard import validate_outbound_url

logger = logging.getLogger(__name__)

#: Platform key this module always stores/reads under -- matches migration
#: 0034's `tenant_platform_credentials.platform` convention.
PLATFORM_DISCORD = "discord"

_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
_TOKEN_URL = "https://discord.com/api/oauth2/token"  # noqa: S105 - URL, not a credential
_BOT_INSTALL_SCOPES = "bot applications.commands"
_ALLOWED_SCHEMES: tuple[str, ...] = ("https",)
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

#: Discord permission bitfield requested at install time -- the minimal
#: set Bar Citizen's own role-sync worker needs (manage roles, read/send
#: messages, manage webhooks), not Administrator. Named constant so a
#: future permission-set change is a one-line diff.
DEFAULT_BOT_PERMISSIONS = 268511312


class DiscordInstallError(ApiError):
    """Raised for any bot-install flow failure the caller should see as a clean 4xx/5xx."""


@dataclass(slots=True, frozen=True)
class InstallResult:
    """Outcome of a completed, credential-persisting bot-install callback -- never a secret."""

    tenant_id: int
    platform: str
    installed_by_user_id: int


async def build_authorize_url(
    *,
    tenant_id: int,
    is_global_tenant: bool,
    admin_user_id: int,
    application_id: str,
    client_secret: str,
    bot_token: str | None,
    redirect_uri: str,
    permissions: int = DEFAULT_BOT_PERMISSIONS,
) -> str:
    """Build the tenant's own Discord bot-install authorize URL.

    Raises 400 (via `bad_request`) for the global tenant -- it uses the
    shared SaaS Discord application, never its own, same fail-fast
    precedent `store_tenant_credentials()` applies at persist time
    (`services.credential_resolver`'s own docstring); raises 400 for a
    blank `application_id`/`client_secret`.
    """
    if is_global_tenant:
        raise bad_request("the global tenant cannot install its own Discord app")
    if not application_id or not client_secret:
        raise bad_request("application_id and client_secret are required")

    state = await create_state(
        tenant_id=tenant_id,
        admin_user_id=admin_user_id,
        application_id=application_id,
        client_secret=client_secret,
        bot_token=bot_token,
        redirect_uri=redirect_uri,
    )
    params = {
        "client_id": application_id,
        "scope": _BOT_INSTALL_SCOPES,
        "permissions": str(permissions),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }
    return f"{_AUTHORIZE_URL}?{urlencode(params)}"


async def _exchange_code(
    *, application_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict[str, Any]:
    """POST the authorization code to Discord's token endpoint using the tenant's own app.

    Raises `DiscordInstallError` on any transport failure, non-2xx, or
    malformed response -- message carries only the HTTP status, never a
    token, secret, or raw response body (`security.md` Token & Secret
    Hygiene).
    """
    try:
        await validate_outbound_url(_TOKEN_URL, allowed_schemes=_ALLOWED_SCHEMES)
    except ApiError as exc:
        raise DiscordInstallError(f"outbound URL blocked: {exc.message}", 502) from exc

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                _TOKEN_URL,
                data={
                    "client_id": application_id,
                    "client_secret": client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.HTTPError as exc:
        logger.warning("discord_install_service.token_request_failed")
        raise DiscordInstallError("discord token exchange failed: network error", 502) from exc

    if response.status_code // 100 != 2:
        logger.warning(
            "discord_install_service.token_request_non_2xx status=%d", response.status_code
        )
        raise DiscordInstallError(
            f"discord token exchange failed: HTTP {response.status_code}", 502
        )

    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise DiscordInstallError("discord token exchange returned malformed JSON", 502) from exc
    if not isinstance(payload, dict):
        raise DiscordInstallError("discord token exchange returned malformed JSON", 502)
    return payload


async def complete_install(dal: Any, *, code: str, state: str) -> InstallResult:
    """Verify `state`, exchange `code`, then persist the now-verified tenant Discord app.

    Raises `DiscordInstallError` (400) for a missing authorization code or
    a forged/expired/replayed/corrupt `state` -- the single generic
    message avoids a state-guessing oracle, same posture the parked
    Discord bot-install design (`#504`) documented. Never partially
    persists: a failed code exchange raises before
    `store_tenant_credentials()` is ever called.
    """
    if not code:
        raise bad_request("missing authorization code")

    pending = await consume_state(state)
    if pending is None:
        raise DiscordInstallError("invalid, expired, or already-used install state", 400)

    await _exchange_code(
        application_id=pending.application_id,
        client_secret=pending.client_secret,
        code=code,
        redirect_uri=pending.redirect_uri,
    )

    payload: dict[str, Any] = {
        "client_id": pending.application_id,
        "client_secret": pending.client_secret,
    }
    if pending.bot_token:
        payload["bot_token"] = pending.bot_token

    store_tenant_credentials(
        dal,
        tenant_id=pending.tenant_id,
        is_global_tenant=False,
        platform=PLATFORM_DISCORD,
        payload=payload,
        installed_by_user_id=pending.admin_user_id,
    )

    return InstallResult(
        tenant_id=pending.tenant_id,
        platform=PLATFORM_DISCORD,
        installed_by_user_id=pending.admin_user_id,
    )
