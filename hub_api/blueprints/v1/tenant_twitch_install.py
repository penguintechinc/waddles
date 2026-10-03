"""v1 `tenant.twitch_install` group -- per-tenant Twitch bot-install OAuth flow (Bar Citizen).

**Net-new, Twitch-only half of the Bar Citizen OAuth bot-install work** --
the sibling unit building the Discord half (`tenant_discord_install.py`,
if/when it lands) is a separate file; this module never imports it and
never shares a blueprint object with it, avoiding any merge collision
between the two stacked PRs.

Each tenant beyond tenant 0 brings its OWN Twitch application (client
id/secret, registered by the tenant admin in the Twitch developer
console) and authorizes a bot account for the exact Helix scopes the
Bar Citizen role-sync worker needs
(`services.twitch_install_oauth.ROLE_SYNC_SCOPES`) -- tenant 0 keeps using
the SaaS-wide Twitch app via `services.credential_resolver`'s existing
`is_global_tenant` lane and is explicitly rejected here (`tenant:admin`
on an `is_default` tenant always 400s, matching `credential_resolver.
store_tenant_credentials`'s own DB-level backstop).

Two routes, same tenant-vs-public split every other OAuth flow in this
port uses (see `blueprints/v1/community_connections.py`'s own module
docstring for the precedent this mirrors):

- `POST /api/v1/tenant/twitch-install/authorize` -- tenant-admin-scoped
  (`tenant_middleware` -> `require_scope("tenant:admin")`, security.md's
  tenant-before-scope ordering). Takes the tenant's own Twitch
  `client_id`/`client_secret` in the body, mints a single-use CSRF
  `state` token (`services.twitch_install_state`) carrying that
  (encrypted) context, and returns Twitch's authorize URL.
- `GET /api/v1/tenant/twitch-install/callback` -- PUBLIC, no auth
  decorator. Twitch's own redirect target; the browser carries no bearer
  token here, so the single-use `state` token is the ONLY thing binding
  this request back to a tenant/admin -- never trust `tenant_id` from a
  query/body value on this route, there isn't one. Exchanges the code,
  stores the resulting credentials (encrypted, via
  `services.twitch_install_credentials.store_initial_credentials`), and
  renders the same `window.opener` postMessage popup-closing page
  `community_connections.py`'s callback uses.

Feature-gated behind `waddles.community.guild_pairing` (same flag the
guild-pairing CRUD group uses -- this is the install-time half of the
same Professional+ Bar Citizen feature, not a second gate to keep in
sync).

Never logs a token, code, `client_id`, or `client_secret` anywhere in this
module -- failures log tenant id and a fixed error code only.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, Response, current_app, request
from quart_schema import validate_response

from config import HubAPIConfig
from services import twitch_install_credentials as creds_svc
from services import twitch_install_oauth as oauth_svc
from services import twitch_install_state as state_svc
from services.current_user import get_current_user_id
from services.errors import ApiError, bad_request, forbidden, payment_required

logger = logging.getLogger(__name__)

tenant_twitch_install_bp = Blueprint(
    "v1_tenant_twitch_install", __name__, url_prefix="/api/v1/tenant/twitch-install"
)

#: Same two-gate feature flag the guild-pairing CRUD group uses -- see module docstring.
FEATURE_TWITCH_INSTALL = "waddles.community.guild_pairing"

#: Upper bound on tenant-admin-suppliable `client_id`/`client_secret` length --
#: Twitch's own values are short (~30 chars); this is a sanity ceiling against
#: an oversized body, not a precise format check (Twitch's token endpoint is
#: the real validator).
_MAX_CREDENTIAL_LENGTH = 512


# ---------------------------------------------------------------------------
# Response DTOs (quart-schema `@validate_response` targets -- security.md
# Output Validation: never a raw dict/model serialized directly)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class TwitchInstallAuthorizeResponse:
    """Response envelope for the authorize-initiation route."""

    success: bool
    authorize_url: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dal() -> Any:
    return current_app.config["dal"]


def _cfg() -> HubAPIConfig:
    return cast(HubAPIConfig, current_app.config["HUB_API_CONFIG"])


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _redirect_uri() -> str:
    """Fixed, server-configured callback URL -- never client-suppliable (anti open-redirect)."""
    return f"{_cfg().connections_callback_base_url}/api/v1/tenant/twitch-install/callback"


def _callback_html(*, ok: bool, error: str | None = None) -> Response:
    """Popup-closing HTML page for the callback route -- mirrors `community_connections.py`'s own.

    Posts `{type: 'TWITCH_INSTALL_CALLBACK', ok, error?}` to `window.
    opener` (scoped to this origin, never `'*'`) then closes the window.
    NEVER embeds a token, code, state, client_id, or client_secret --
    `error` is one of this module's own short, fixed string constants.
    """
    message: dict[str, Any] = {"type": "TWITCH_INSTALL_CALLBACK", "ok": ok}
    if error is not None:
        message["error"] = error
    body = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Connecting Twitch...</title></head>
<body>
<p>You can close this window.</p>
<script>
(function () {{
  var message = {json.dumps(message)};
  if (window.opener) {{
    window.opener.postMessage(message, window.location.origin);
  }}
  window.close();
}})();
</script>
</body>
</html>"""
    return Response(
        body,
        mimetype="text/html",
        headers={"Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'"},
    )


def _validate_credential_field(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise bad_request(f"{field_name} is required")
    if len(value) > _MAX_CREDENTIAL_LENGTH:
        raise bad_request(f"{field_name} is too long")
    return value


# ---------------------------------------------------------------------------
# Tenant-admin: start the install flow
# ---------------------------------------------------------------------------


@tenant_twitch_install_bp.route("/authorize", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_response(TwitchInstallAuthorizeResponse)
async def authorize() -> TwitchInstallAuthorizeResponse | tuple[dict[str, object], int]:
    """`POST /api/v1/tenant/twitch-install/authorize`.

    Body: `{"client_id": "...", "client_secret": "..."}` -- the tenant's
    own Twitch application credentials. Mints a single-use `state` token
    and returns Twitch's authorize URL; the tenant admin's browser
    navigates there directly (never proxied through hub-api).
    """
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware always runs first

    if not await feature_enabled(FEATURE_TWITCH_INSTALL, tenant=ctx.tenant_slug):
        return _err(payment_required("Twitch install requires a Professional plan or higher"))

    if ctx.is_default:
        return _err(forbidden("The global tenant cannot install a per-tenant Twitch app"))

    body = await request.get_json(force=True, silent=True) or {}
    if not isinstance(body, dict):
        return _err(bad_request("Request body must be a JSON object"))

    try:
        client_id = _validate_credential_field(body.get("client_id"), field_name="client_id")
        client_secret = _validate_credential_field(
            body.get("client_secret"), field_name="client_secret"
        )
    except ApiError as exc:
        return _err(exc)

    user_id = get_current_user_id(request)
    redirect_uri = _redirect_uri()
    state = await state_svc.create_state(
        tenant_id=ctx.tenant_id,
        installed_by_user_id=user_id,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
    )

    authorize_url = oauth_svc.build_authorize_url(
        client_id=client_id, redirect_uri=redirect_uri, state=state
    )
    return TwitchInstallAuthorizeResponse(success=True, authorize_url=authorize_url)


# ---------------------------------------------------------------------------
# Public: OAuth callback (no auth -- see module docstring)
# ---------------------------------------------------------------------------


@tenant_twitch_install_bp.route("/callback", methods=["GET"])
async def callback() -> tuple[Response, int] | Response:
    """`GET /api/v1/tenant/twitch-install/callback` -- PUBLIC, Twitch's own redirect target.

    Never logs or embeds `code`/`state`/`client_secret`/token values
    anywhere in the response or logs (module docstring, `_callback_html`'s
    own docstring).
    """
    state_param = request.args.get("state", "")
    code = request.args.get("code")
    provider_error = request.args.get("error")

    payload = await state_svc.consume_state(state_param)
    if payload is None:
        return _callback_html(ok=False, error="invalid_or_expired_state"), 400

    if provider_error:
        return _callback_html(ok=False, error="access_denied")

    if not code:
        return _callback_html(ok=False, error="missing_code"), 400

    try:
        token = await oauth_svc.exchange_code(
            client_id=payload.client_id,
            client_secret=payload.client_secret,
            code=code,
            redirect_uri=payload.redirect_uri,
        )
    except oauth_svc.TwitchOAuthError:
        logger.warning("tenant_twitch_install.exchange_failed tenant_id=%s", payload.tenant_id)
        return _callback_html(ok=False, error="exchange_failed")

    try:
        creds_svc.store_initial_credentials(
            _dal(),
            tenant_id=payload.tenant_id,
            client_id=payload.client_id,
            client_secret=payload.client_secret,
            token=token,
            installed_by_user_id=payload.installed_by_user_id,
        )
    except ApiError:
        logger.warning("tenant_twitch_install.store_failed tenant_id=%s", payload.tenant_id)
        return _callback_html(ok=False, error="save_failed")

    return _callback_html(ok=True)


BLUEPRINTS: list[Blueprint] = [tenant_twitch_install_bp]
