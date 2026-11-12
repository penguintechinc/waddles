"""v1 `tenant.discord_install` group -- per-tenant Discord OAuth2 bot-install flow.

Two routes, same asymmetric-gating precedent the parked Discord
bot-install design (`#504`) documented for its own blueprint: the
tenant-admin-facing authorize route sits behind `tenant_middleware` ->
`require_scope` (security.md: tenant before scope) plus the two-gate
`feature_enabled()` check; the public callback Discord's own browser
redirect lands on carries no bearer token at all, so the HMAC-free but
still single-use, short-TTL `state` token
(`services.tenant_discord_install_state`) is the actual gate -- verified
and consumed before anything else happens, same ordering
`services.discord_install_service.complete_install()` enforces.

Matches the discovery contract every v1 port group follows: a module-
level `BLUEPRINTS: list[Blueprint]`, found and mounted by auto-discovery
-- no edit to `routers/v1.py`/`blueprints/__init__.py` needed.

Scope resolution deliberately stops short of activating a
`guild_tenant_pairings` row -- that's Unit F's role-sync worker's own
concern, sitting on top of `#563`'s `services/guild_pairing.py`. This
group's entire job is getting a verified, encrypted, per-tenant Discord
app into `tenant_platform_apps` (layer 1 of migration 0035's three-layer
connection model, renamed from migration 0034's `tenant_platform_
credentials`) via `services.credential_resolver.store_tenant_credentials()`.
This flow stores ONLY app-level credentials (`client_id`/`client_secret`/
`bot_token`) -- it never captures a `guild_id`, so it has no layer-2
`platform_connections` row to create; see that module's own docstring for
why (a Discord bot token is app-scoped, not guild-scoped, and this OAuth
callback's contract doesn't carry a resource identifier today).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlencode

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, after_this_request, current_app, redirect, request
from quart_schema import validate_response

from services import discord_server_install as server_install_svc
from services import install_state_binding as binding
from services.current_user import get_current_user_id
from services.discord_install_service import (
    DiscordInstallError,
    build_authorize_url,
    complete_install,
)
from services.errors import ApiError, bad_request, unauthorized
from services.tenant_discord_install_state import DEFAULT_TTL_SECONDS

logger = logging.getLogger(__name__)

tenant_discord_install_bp = Blueprint("v1_tenant_discord_install", __name__, url_prefix="/api/v1")

#: Two-gate feature flag -- this port's `waddles.<module>.<feature>` convention
#: (`waddles.community.guild_pairing`, ...). Gates ONLY the admin-initiated
#: authorize route: a disabled flag means no `state` is ever minted, which
#: already blocks the public callback from doing anything (it has no valid
#: `state` to consume) -- no separate gate needed on the public route
#: itself.
FEATURE_TENANT_DISCORD_INSTALL = "waddles.tenant.discord_install"

#: ``__Host-`` cookie carrying the initiating browser's state-binding nonce.
BINDING_COOKIE = binding.cookie_name("dc_install")


@dataclass(slots=True, frozen=True)
class AuthorizeUrlResponse:
    """`authorize_url` response envelope -- never includes the submitted secret."""

    success: bool
    authorize_url: str


@dataclass(slots=True, frozen=True)
class InstallCallbackResponse:
    """Completed-install response envelope -- identifiers only, never a credential."""

    success: bool
    tenant_id: int
    platform: str


def _dal() -> Any:
    return current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _redirect_uri() -> str:
    cfg = current_app.config.get("HUB_API_CONFIG")
    base = str(getattr(cfg, "identity_callback_base_url", "") or "").rstrip("/")
    return f"{base}/api/v1/discord/install/callback"


@tenant_discord_install_bp.route("/tenants/discord/install/authorize", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("tenant.discord_install:write")  # type: ignore[misc]
@validate_response(AuthorizeUrlResponse)
async def start_install() -> AuthorizeUrlResponse | tuple[dict[str, object], int]:
    """`POST /api/v1/tenants/discord/install/authorize`.

    Body: `{"application_id": str, "client_secret": str, "bot_token": str|null}`.
    Tenant 0 is rejected here -- mirrors migration 0034's own
    `trg_reject_global_tenant_credentials` trigger, before a `state` token
    is even minted (the global tenant uses the shared SaaS Discord app,
    resolved by `CredentialResolver`, never its own).
    """
    ctx = get_tenant_context(request)
    if ctx is None:
        return _err(unauthorized("tenant context missing"))
    if not await feature_enabled(FEATURE_TENANT_DISCORD_INSTALL, tenant=ctx.tenant_slug):
        return {
            "success": False,
            "error": "Discord bot-install requires a Professional plan or higher",
        }, 402
    if ctx.is_default:
        return _err(bad_request("the global tenant cannot install its own Discord app"))

    body = await request.get_json(force=True, silent=True) or {}
    if not isinstance(body, dict):
        return _err(bad_request("Request body must be a JSON object"))

    application_id = body.get("application_id")
    client_secret = body.get("client_secret")
    bot_token = body.get("bot_token")
    if not isinstance(application_id, str) or not isinstance(client_secret, str):
        return _err(bad_request("application_id and client_secret are required strings"))
    if bot_token is not None and not isinstance(bot_token, str):
        return _err(bad_request("bot_token, if given, must be a string"))

    actor_id = get_current_user_id(request)
    nonce = binding.new_nonce()
    try:
        authorize_url = await build_authorize_url(
            tenant_id=ctx.tenant_id,
            is_global_tenant=ctx.is_default,
            admin_user_id=actor_id,
            application_id=application_id,
            client_secret=client_secret,
            bot_token=bot_token,
            redirect_uri=_redirect_uri(),
            binding_nonce=nonce,
        )
    except ApiError as exc:
        logger.warning("discord install authorize failed: %s", type(exc).__name__)
        return _err(exc)

    @after_this_request
    def _bind_browser(response: Any) -> Any:
        """Bind the state to THIS browser (``__Host-`` HttpOnly Secure SameSite=Lax cookie)."""
        binding.set_binding_cookie(response, BINDING_COOKIE, nonce, DEFAULT_TTL_SECONDS)
        return response

    return AuthorizeUrlResponse(success=True, authorize_url=authorize_url)


@tenant_discord_install_bp.route("/discord/install/callback", methods=["GET"])
@validate_response(InstallCallbackResponse)
async def install_callback() -> InstallCallbackResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/discord/install/callback` -- PUBLIC, no auth decorator.

    Discord's own browser redirect carries no bearer token; the single-
    use, short-TTL `state` token is the actual gate -- a forged, expired,
    or replayed `state` is rejected inside `complete_install()` before any
    DB write happens (fail-closed on exchange errors too: a failed code
    exchange never calls `store_tenant_credentials()`).
    """
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    if state.startswith(server_install_svc.STATE_PREFIX):
        return await _server_install_callback(code, state)

    @after_this_request
    def _clear_binding(response: Any) -> Any:
        """Expire the binding cookie once the install completed."""
        if response.status_code < 400:
            binding.clear_binding_cookie(response, BINDING_COOKIE)
        return response

    try:
        result = await complete_install(
            _dal(), code=code, state=state, binding_nonce=request.cookies.get(BINDING_COOKIE)
        )
    except DiscordInstallError as exc:
        logger.warning("discord install callback failed: %s", type(exc).__name__)
        return _err(exc)
    except ApiError as exc:
        logger.warning("discord install callback failed: %s", type(exc).__name__)
        return _err(exc)

    return InstallCallbackResponse(
        success=True, tenant_id=result.tenant_id, platform=result.platform
    )


#: Must match `blueprints.v1.guild_pairing.SERVER_INSTALL_BINDING_COOKIE`.
SERVER_INSTALL_BINDING_COOKIE = binding.cookie_name("dc_server_install")


async def _server_install_callback(code: str, state: str) -> Any:
    """Add-Discord-server leg of the shared callback: record install + pair, then redirect.

    Always redirects the browser back to the webui (never a JSON page): success lands on the
    community's role-sync page with `discord_server=paired`, failures carry a fixed reason code.
    """
    cfg = current_app.config.get("HUB_API_CONFIG")
    origin = str(getattr(cfg, "frontend_origin", "") or "").rstrip("/")
    nonce = request.cookies.get(SERVER_INSTALL_BINDING_COOKIE)
    if request.args.get("error"):
        # User cancelled on Discord: burn the state (if ours) to learn where to send them.
        try:
            pending = await server_install_svc.consume_server_state(state, nonce)
            target = f"{origin}/admin/{pending.community_id}/role-sync"
        except Exception:  # noqa: BLE001 - any state failure still returns the browser home
            target = f"{origin}/dashboard"
        return redirect(f"{target}?{urlencode({'discord_server': 'error', 'reason': 'cancelled'})}")
    try:
        result = await server_install_svc.complete_server_install(
            _dal(),
            code=code,
            state=state,
            guild_id_param=request.args.get("guild_id") or None,
            binding_nonce=nonce,
        )
    except server_install_svc.ServerInstallError as exc:
        logger.warning("discord server install callback failed: code=%s", exc.code)
        target = (
            f"{origin}/admin/{exc.community_id}/role-sync"
            if exc.community_id is not None
            else f"{origin}/dashboard"
        )
        return redirect(f"{target}?{urlencode({'discord_server': 'error', 'reason': exc.code})}")
    except ApiError as exc:
        logger.warning("discord server install callback failed: code=%s", exc.code)
        return redirect(
            f"{origin}/dashboard?{urlencode({'discord_server': 'error', 'reason': exc.code})}"
        )
    page = redirect(
        f"{origin}/admin/{result.community_id}/role-sync?"
        + urlencode({"discord_server": "paired", "guild_id": result.guild_id})
    )
    binding.clear_binding_cookie(page, SERVER_INSTALL_BINDING_COOKIE)
    return page


BLUEPRINTS: list[Blueprint] = [tenant_discord_install_bp]
