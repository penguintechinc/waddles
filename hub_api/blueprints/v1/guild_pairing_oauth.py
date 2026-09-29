"""v1 `guild_pairing.oauth` group -- per-tenant platform app creds + Discord bot-install OAuth2.

Three surfaces, same split every other Community-module OAuth blueprint in
this port uses (`community_connections.py`'s own docstring is the
precedent this follows):

- `guild_discord_app_bp` (`/api/v1/admin/<platform>/app` -- PLATFORM-GENERIC,
  see `SUPPORTED_PLATFORMS`; `/api/v1/admin/discord/install/start` --
  Discord-specific): tenant-JWT admin -- set/rotate credentials
  (write-only, never returns a secret), masked GET, and the Discord
  authorize-URL builder. `tenant_middleware` -> `require_scope` chain,
  same as every other admin route in this port. Owner clarification
  (2026-09-29): every non-global tenant needs its own app per platform,
  not only Discord -- the credential-storage routes are generic across
  `SUPPORTED_PLATFORMS`; the OAuth *connect flow* (authorize/callback,
  pairing activation) is implemented for Discord only today, tracked in
  `services/platform_oauth_connectors.py`'s registry.
- `guild_discord_app_bp` also owns the ONE public, unauthenticated route
  in this file: `GET /api/v1/discord/install/callback`. Discord redirects
  the admin's own browser here directly -- no bearer token at all -- so it
  cannot sit behind `tenant_middleware`/`require_scope`; the HMAC-signed,
  single-use `state` token (`services/guild_oauth_state.py`) is the actual
  gate that matters here, verified before anything else happens.
- `guild_discord_internal_bp` (`/api/v1/internal/discord/...`):
  service-to-service `X-Service-Key` only, same `is_valid_service_key()`
  mechanism as every other internal blueprint in this port
  (`community_activity.py`'s `activity_internal_bp`, etc.) -- credential
  resolution for the data plane (already platform-generic, `platform` is
  a body field), and pairing revocation for a Discord
  guild-delete/integration-removed gateway event (contract Sec5).

Feature-gated (`waddles.guild-pairing`, default OFF) on every
tenant-admin-facing route AND the public callback (checked against the
`state`-verified tenant, so a disabled flag still rejects a completed
install rather than only hiding the start button) -- NOT on the internal
routes, matching `community_connections.py`'s own asymmetric-gating
precedent (internal machinery ungated, admin-facing surface gated).
"""

from __future__ import annotations

from typing import Any, cast

from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request

from services.community_common import is_valid_service_key
from services.current_user import get_current_user_id
from services.errors import ApiError, bad_request, unauthorized
from services.guild_credential_resolution import CredentialResolutionError, resolve_credentials
from services.guild_oauth_install_service import (
    OAuthInstallError,
    build_authorize_url,
    complete_install,
    revoke_pairing,
)
from services.tenant_platform_credentials_service import (
    get_masked_credentials,
    set_credentials,
)

guild_discord_app_bp = Blueprint("v1_guild_discord_app", __name__, url_prefix="/api/v1")

#: Service-to-service only -- see module docstring.
guild_discord_internal_bp = Blueprint(
    "v1_guild_discord_internal", __name__, url_prefix="/api/v1/internal/discord"
)

BLUEPRINTS: list[Blueprint] = [guild_discord_app_bp, guild_discord_internal_bp]

#: Flag key per the task brief -- default OFF until validated.
FEATURE_GUILD_PAIRING = "waddles.guild-pairing"

#: Discord permission bitfield requested at install time -- the minimal
#: set this port's own bundle/connector surface needs (manage roles, read/
#: send messages, manage webhooks) rather than Administrator. Kept as a
#: named constant so a future permission-set change is a one-line diff,
#: not a magic number scattered across call sites.
_DEFAULT_BOT_PERMISSIONS = 268511312


def _install_dal() -> Any:
    return current_app.config["install_dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return {"success": False, "error": {"code": exc.code, "message": exc.message}}, exc.status_code


def _callback_base_url() -> str:
    cfg = current_app.config.get("HUB_API_CONFIG")
    base = getattr(cfg, "identity_callback_base_url", "") if cfg is not None else ""
    return str(base).rstrip("/")


def _redirect_uri() -> str:
    return f"{_callback_base_url()}/api/v1/discord/install/callback"


@guild_discord_app_bp.route("/admin/<platform>/app", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("guild_pairing:write")  # type: ignore[untyped-decorator]
async def set_platform_app(platform: str) -> tuple[dict[str, object], int]:
    """`POST /api/v1/admin/<platform>/app` -- set/rotate this tenant's own app credentials.

    Platform-generic storage (owner clarification 2026-09-29): every
    tenant other than the global tenant needs its own app integration for
    every platform it connects, not only Discord. `bot_token`/
    `extra_secret` are optional -- not every platform's credential has a
    separate bot token or extra secret (`SUPPORTED_PLATFORMS` documents
    which platforms this table accepts; the OAuth *connect flow* is
    Discord-only today, see `services/platform_oauth_connectors.py`).
    """
    ctx = get_tenant_context(request)
    if ctx is None:
        return _err(unauthorized("tenant context missing"))
    if not await feature_enabled(FEATURE_GUILD_PAIRING, tenant=ctx.tenant_slug):
        return {"success": False, "error": "Guild pairing is not enabled for this plan"}, 402

    payload = await request.get_json(force=True, silent=True) or {}
    application_id = payload.get("application_id")
    client_secret = payload.get("client_secret")
    bot_token = payload.get("bot_token")
    extra_secret = payload.get("extra_secret")
    if not isinstance(application_id, str) or not isinstance(client_secret, str):
        return _err(bad_request("application_id and client_secret are required strings"))
    if bot_token is not None and not isinstance(bot_token, str):
        return _err(bad_request("bot_token, if given, must be a string"))
    if extra_secret is not None and not isinstance(extra_secret, str):
        return _err(bad_request("extra_secret, if given, must be a string"))

    actor_id = get_current_user_id(request)
    try:
        result = await set_credentials(
            _install_dal(),
            tenant_id=ctx.tenant_id,
            actor_id=actor_id,
            platform=platform,
            application_id=application_id,
            client_secret=client_secret,
            bot_token=bot_token,
            extra_secret=extra_secret,
        )
    except ApiError as exc:
        return _err(exc)

    return {
        "success": True,
        "data": {
            "platform": result.platform,
            "application_id": result.application_id,
            "client_secret_hint": result.client_secret_hint,
            "bot_token_hint": result.bot_token_hint,
            "extra_secret_hint": result.extra_secret_hint,
            "is_active": result.is_active,
            "updated_at": result.updated_at.isoformat(),
        },
    }, 200


@guild_discord_app_bp.route("/admin/<platform>/app", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("guild_pairing:read")  # type: ignore[untyped-decorator]
async def get_platform_app(platform: str) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<platform>/app` -- masked hints + timestamps only, never a secret."""
    ctx = get_tenant_context(request)
    if ctx is None:
        return _err(unauthorized("tenant context missing"))
    if not await feature_enabled(FEATURE_GUILD_PAIRING, tenant=ctx.tenant_slug):
        return {"success": False, "error": "Guild pairing is not enabled for this plan"}, 402

    try:
        result = await get_masked_credentials(
            _install_dal(), tenant_id=ctx.tenant_id, platform=platform
        )
    except ApiError as exc:
        return _err(exc)

    return {
        "success": True,
        "data": {
            "platform": result.platform,
            "application_id": result.application_id,
            "client_secret_hint": result.client_secret_hint,
            "bot_token_hint": result.bot_token_hint,
            "extra_secret_hint": result.extra_secret_hint,
            "is_active": result.is_active,
            "created_at": result.created_at.isoformat(),
            "updated_at": result.updated_at.isoformat(),
        },
    }, 200


@guild_discord_app_bp.route("/admin/discord/install/start", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("guild_pairing:write")  # type: ignore[untyped-decorator]
async def start_install() -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/discord/install/start` -- build this tenant's Discord authorize URL."""
    ctx = get_tenant_context(request)
    if ctx is None:
        return _err(unauthorized("tenant context missing"))
    if not await feature_enabled(FEATURE_GUILD_PAIRING, tenant=ctx.tenant_slug):
        return {"success": False, "error": "Guild pairing is not enabled for this plan"}, 402

    actor_id = get_current_user_id(request)
    try:
        authorize_url = await build_authorize_url(
            _install_dal(),
            tenant_id=ctx.tenant_id,
            admin_user_id=actor_id,
            redirect_uri=_redirect_uri(),
            permissions=_DEFAULT_BOT_PERMISSIONS,
        )
    except ApiError as exc:
        return _err(exc)

    return {"success": True, "data": {"authorize_url": authorize_url}}, 200


@guild_discord_app_bp.route("/discord/install/callback", methods=["GET"])
async def install_callback() -> tuple[dict[str, object], int]:
    """`GET /api/v1/discord/install/callback` -- PUBLIC, no auth decorator.

    Discord's own browser redirect carries no bearer token; the
    HMAC-signed, single-use `state` token is the actual gate. Forged,
    expired, or replayed state is rejected inside `complete_install()`
    before any DB write happens.
    """
    state = request.args.get("state", "")
    code = request.args.get("code", "")
    guild_id = request.args.get("guild_id", "")
    permissions_raw = request.args.get("permissions")
    permissions = int(permissions_raw) if permissions_raw and permissions_raw.isdigit() else None

    if not code:
        return _err(bad_request("missing authorization code"))

    try:
        result = await complete_install(
            _install_dal(),
            state=state,
            code=code,
            guild_id=guild_id,
            permissions=permissions,
            redirect_uri=_redirect_uri(),
        )
    except OAuthInstallError as exc:
        return _err(exc)
    except ApiError as exc:
        return _err(exc)

    return {
        "success": True,
        "data": {
            "pairing_id": result.pairing_id,
            "guild_id": result.guild_id,
            "tenant_id": result.tenant_id,
        },
    }, 200


# ===== Internal (service-to-service, X-Service-Key) =====


@guild_discord_internal_bp.route("/credentials/resolve", methods=["POST"])
async def resolve_discord_credentials() -> tuple[dict[str, object], int]:
    """`POST /api/v1/internal/discord/credentials/resolve` -- data-plane bot-credential resolution.

    Body: `{"tenant_id": int, "platform": "discord"}`. Fail-closed per
    contract Sec2: a non-global tenant with no configured app gets a 409,
    NEVER the platform bot.
    """
    if not is_valid_service_key(request):
        return {"success": False, "error": "Invalid service key"}, 401

    payload = await request.get_json(force=True, silent=True) or {}
    tenant_id = payload.get("tenant_id")
    platform = payload.get("platform", "discord")
    if not isinstance(tenant_id, int):
        return _err(bad_request("tenant_id (int) is required"))

    try:
        resolved = await resolve_credentials(_install_dal(), tenant_id=tenant_id, platform=platform)
    except CredentialResolutionError as exc:
        return _err(exc)
    except ApiError as exc:
        return _err(exc)

    return {
        "success": True,
        "data": {
            "tenant_id": resolved.tenant_id,
            "platform": resolved.platform,
            "is_platform_bot": resolved.is_platform_bot,
            "bot_token": resolved.bot_token,
        },
    }, 200


@guild_discord_internal_bp.route("/pairings/revoke", methods=["POST"])
async def revoke_discord_pairing() -> tuple[dict[str, object], int]:
    """`POST /api/v1/internal/discord/pairings/revoke` -- guild-delete/integration-removed event.

    Body: `{"platform": str, "guild_id": str, "tenant_id": int,
    "revoked_by": "guild_removed_bot"|"integration_removed"}` (contract
    Sec5). Never accepts `"tenant_admin"` here -- that path is the
    tenant-JWT-authenticated admin route, not this service-to-service one
    (kept as a documented follow-up rather than added speculatively here).
    """
    if not is_valid_service_key(request):
        return {"success": False, "error": "Invalid service key"}, 401

    payload = await request.get_json(force=True, silent=True) or {}
    platform = payload.get("platform")
    guild_id = payload.get("guild_id")
    tenant_id = payload.get("tenant_id")
    revoked_by = payload.get("revoked_by")
    if (
        not isinstance(platform, str)
        or not isinstance(guild_id, str)
        or not isinstance(tenant_id, int)
        or revoked_by not in ("guild_removed_bot", "integration_removed")
    ):
        return _err(
            bad_request(
                "platform (str), guild_id (str), tenant_id (int), and revoked_by "
                "(guild_removed_bot|integration_removed) are required"
            )
        )

    try:
        await revoke_pairing(
            _install_dal(),
            platform=platform,
            guild_id=guild_id,
            tenant_id=tenant_id,
            revoked_by=cast(str, revoked_by),
            revoked_by_user_id=None,
        )
    except ApiError as exc:
        return _err(exc)

    return {"success": True}, 200
