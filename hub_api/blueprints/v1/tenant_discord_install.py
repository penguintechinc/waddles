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

from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_response

from services.current_user import get_current_user_id
from services.discord_install_service import (
    DiscordInstallError,
    build_authorize_url,
    complete_install,
)
from services.errors import ApiError, bad_request, unauthorized

tenant_discord_install_bp = Blueprint("v1_tenant_discord_install", __name__, url_prefix="/api/v1")

#: Two-gate feature flag -- this port's `waddles.<module>.<feature>` convention
#: (`waddles.community.guild_pairing`, ...). Gates ONLY the admin-initiated
#: authorize route: a disabled flag means no `state` is ever minted, which
#: already blocks the public callback from doing anything (it has no valid
#: `state` to consume) -- no separate gate needed on the public route
#: itself.
FEATURE_TENANT_DISCORD_INSTALL = "waddles.tenant.discord_install"


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
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant.discord_install:write")  # type: ignore[untyped-decorator]
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
    try:
        authorize_url = await build_authorize_url(
            tenant_id=ctx.tenant_id,
            is_global_tenant=ctx.is_default,
            admin_user_id=actor_id,
            application_id=application_id,
            client_secret=client_secret,
            bot_token=bot_token,
            redirect_uri=_redirect_uri(),
        )
    except ApiError as exc:
        return _err(exc)

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

    try:
        result = await complete_install(_dal(), code=code, state=state)
    except DiscordInstallError as exc:
        return _err(exc)
    except ApiError as exc:
        return _err(exc)

    return InstallCallbackResponse(
        success=True, tenant_id=result.tenant_id, platform=result.platform
    )


BLUEPRINTS: list[Blueprint] = [tenant_discord_install_bp]
