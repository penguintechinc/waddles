"""v1 `community.connections` group -- per-community OAuth "Connections" page (gh-320, chunk C3).

**Not the same resource as `blueprints/v1/ingest_sources.py`** (`/api/v1/
ingest-sources`, spec Sec10.3) -- that blueprint is a per-TENANT registry
of inbound event sources over the `ingest_sources` table (no credentials
ever stored, read by the Rust data plane); this one is the per-COMMUNITY
OAuth token store below (`platform_integrations` table, stores encrypted
access/refresh tokens). Kept as separate resources on separate paths
rather than unified -- see that module's own docstring for the full note.

Three surfaces, mirroring `community_loyalty.py`'s split (see that
module's own docstring for the pattern this follows):

- `connections_bp` (`/api/v1/communities/<id>/connections/...`): tenant-JWT
  admin CRUD -- list/authorize/delete -- same `tenant_middleware` ->
  `require_scope` chain every other Community-module admin route uses.
- `connections_bp` also owns the ONE public, unauthenticated route in this
  file: `GET /api/v1/connections/callback/<provider>`. The user's browser
  (not an API caller) lands here directly from the OAuth provider's
  redirect, carrying no bearer token at all -- it cannot go behind
  `tenant_middleware`/`require_scope`. It shares `connections_bp`'s own
  `url_prefix="/api/v1"` (routes below give relative sub-paths, same
  shape as every other blueprint in this port) purely so this one file
  has a single `BLUEPRINTS` list; it carries NO auth decorator, which is
  the actual gate that matters.
- `connections_internal_bp` (`/api/v1/internal/communities/<id>/
  connections/<provider>/token`): service-to-service `X-Service-Key` only,
  same `is_valid_service_key()` mechanism as `community_loyalty.py`'s
  `loyalty_internal_bp`. Deliberately under `/api/v1/internal/...`, not
  the `/internal/v1/...` shape this chunk's own task brief sketched --
  every existing internal blueprint in this port (`loyalty_internal_bp`,
  `music_internal_bp`, `live_status_internal_bp`,
  `marketplace_admin_review.py`'s internal blueprint,
  `community_interaction.py`'s internal blueprint) uses `/api/v1/
  internal/...`; matching that existing, load-bearing convention was
  judged more valuable than the brief's literal path.

Feature-gated the same asymmetric way `community_loyalty.py` gates
itself (see that module's own docstring): the admin "load the page"
route (`list_connections`, analogous to loyalty's `get_config`) and the
two admin write routes (`authorize_connection`, `delete_connection`) call
`feature_enabled(FEATURE_COMMUNITY_CONNECTIONS, ...)`; the internal
token-fetch route does not, matching loyalty's own internal *read*
routes (`internal_get_balance`/`internal_leaderboard`/`internal_list_
items`) being ungated while only its internal *write* routes gate. Flag
key uses this repo's actual `waddles.<module>.<feature>` convention
(every existing flag in `blueprints/v1/*.py` -- `waddles.community.
loyalty`, `waddles.community.activity`, `waddles.streaming.overlays`,
etc. -- not one uses a `waddlebot.` prefix or a hyphenated feature name),
diverging from the `waddlebot.community-connections` string in this
chunk's own task brief.

The callback route is covered by `services/rate_limiting.py`'s existing
global `before_request` hook (installed once in `app.py`, in front of
EVERY route including this one) -- no per-route rate-limit decorator
exists anywhere in this port to "reuse" instead; the callback's standard-
tier bucket (unauthenticated, so bucketed by client IP -- see that
module's `_rate_limit_identifier()`) already bounds abuse the same way
every other public route in this file's sibling blueprints is bounded.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, Response, current_app, request

from config import HubAPIConfig
from services import community_connections as connections_svc
from services import oauth_connection_state as state_svc
from services import oauth_providers as providers_svc
from services.community_common import community_in_tenant, is_valid_service_key
from services.current_user import get_current_user_id
from services.errors import ApiError, bad_request, not_found, payment_required

connections_bp = Blueprint("v1_community_connections", __name__, url_prefix="/api/v1")

#: Service-to-service only -- see module docstring on the path deviation.
connections_internal_bp = Blueprint(
    "v1_community_connections_internal", __name__, url_prefix="/api/v1/internal"
)

#: Two-gate feature flag -- see module docstring on the `waddles.` prefix.
FEATURE_COMMUNITY_CONNECTIONS = "waddles.community.connections"

#: Refresh a token this many seconds before its recorded expiry -- avoids a
#: caller receiving a token that expires mid-flight to the provider's own API.
_REFRESH_SKEW_S = 60


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config -- tables bound lazily by the service layer."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _cfg() -> HubAPIConfig:
    return cast(HubAPIConfig, current_app.config["HUB_API_CONFIG"])


def _tenant_ok(community_id: int) -> bool:
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    return community_in_tenant(current_app.config["dal"], community_id, ctx)


def _community_exists(dal: Any, community_id: int) -> bool:
    return dal(dal.communities.id == community_id).select(dal.communities.id).first() is not None


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _envelope(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    return {"status": "success", "data": data, "meta": {"version": 1}}, 200


def _callback_redirect_uri(provider: str) -> str:
    return f"{_cfg().connections_callback_base_url}/api/v1/connections/callback/{provider}"


def _callback_html(*, provider: str, ok: bool, error: str | None = None) -> Response:
    """Render the popup-closing HTML page every callback outcome (success or failure) returns.

    Posts `{type: 'OAUTH_CALLBACK', provider, ok, error?}` to `window.
    opener` (scoped to this origin, never `'*'`) then closes the window;
    a visible fallback message covers browsers that block `window.close()`
    on a window the page itself didn't open via `window.open()`. NEVER
    embeds a token, code, or state value -- `error` is one of this
    module's own short, fixed string constants, never provider/exchange
    raw output.
    """
    message: dict[str, Any] = {"type": "OAUTH_CALLBACK", "provider": provider, "ok": ok}
    if error is not None:
        message["error"] = error
    body = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Connecting {provider}...</title></head>
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


# ===== Admin: list =====


@connections_bp.route("/communities/<int:community_id>/connections", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("community.connections:read")  # type: ignore[untyped-decorator]
async def list_connections(community_id: int) -> tuple[dict[str, Any], int]:
    """`GET /api/v1/communities/<id>/connections`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    if not await feature_enabled(FEATURE_COMMUNITY_CONNECTIONS, tenant=ctx.tenant_slug):
        return _err(payment_required("Community connections require a Professional plan or higher"))
    if not _tenant_ok(community_id):
        return _err(not_found("Community not found"))

    async_dal, _dal_ = _dal()
    statuses = await connections_svc.list_connections(async_dal, community_id)
    return _envelope(
        {
            "connections": [status.to_dict() for status in statuses],
            "callback_base": _cfg().connections_callback_base_url,
        }
    )


# ===== Admin: authorize (start OAuth flow) =====


@connections_bp.route(
    "/communities/<int:community_id>/connections/<provider>/authorize", methods=["POST"]
)
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("community.connections:write")  # type: ignore[untyped-decorator]
async def authorize_connection(community_id: int, provider: str) -> tuple[dict[str, Any], int]:
    """`POST /api/v1/communities/<id>/connections/<provider>/authorize`.

    Builds the provider's authorize-redirect URL and stashes single-use
    request context (`services.oauth_connection_state`) keyed by a fresh
    `state` token -- the public callback route below consumes it to
    finish the flow without trusting anything client-suppliable at that
    point (the callback carries no JWT).
    """
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    if not await feature_enabled(FEATURE_COMMUNITY_CONNECTIONS, tenant=ctx.tenant_slug):
        return _err(payment_required("Community connections require a Professional plan or higher"))
    if not _tenant_ok(community_id):
        return _err(not_found("Community not found"))

    try:
        spec = providers_svc.get_provider(provider)
    except ValueError:
        return _err(bad_request(f"Unsupported provider: {provider}"))

    code_verifier: str | None = None
    code_challenge: str | None = None
    if spec.uses_pkce:
        code_verifier, code_challenge = providers_svc.make_pkce_pair()

    redirect_uri = _callback_redirect_uri(provider)
    user_id = get_current_user_id(request)
    state = await state_svc.create_state(
        community_id=community_id,
        provider=provider,
        user_id=user_id,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
    )

    try:
        authorize_url = providers_svc.build_authorize_url(
            provider, redirect_uri=redirect_uri, state=state, code_challenge=code_challenge
        )
    except providers_svc.ProviderNotConfigured:
        return {"error": "provider_not_configured", "provider": provider}, 503

    return _envelope({"authorize_url": authorize_url})


# ===== Public: OAuth callback (no auth -- see module docstring) =====


@connections_bp.route("/connections/callback/<provider>", methods=["GET"])
async def oauth_callback(provider: str) -> tuple[Response, int] | Response:
    """`GET /api/v1/connections/callback/<provider>` -- PUBLIC, the provider's own redirect target.

    Never logs or embeds `code`/`state`/token values anywhere in the
    response (module docstring, `_callback_html`'s own docstring).
    """
    state_param = request.args.get("state", "")
    code = request.args.get("code")
    provider_error = request.args.get("error")

    payload = await state_svc.consume_state(state_param)
    if payload is None:
        return _callback_html(provider=provider, ok=False, error="invalid_or_expired_state"), 400

    if provider_error:
        return _callback_html(provider=provider, ok=False, error="access_denied")

    if not code:
        return _callback_html(provider=provider, ok=False, error="missing_code"), 400

    try:
        token = await providers_svc.exchange_code(
            provider,
            code=code,
            redirect_uri=payload.redirect_uri,
            code_verifier=payload.code_verifier,
        )
    except (ValueError, providers_svc.ProviderNotConfigured, providers_svc.OAuthExchangeError):
        return _callback_html(provider=provider, ok=False, error="exchange_failed")

    expires_at = (
        datetime.now(UTC) + timedelta(seconds=token.expires_in)
        if token.expires_in is not None
        else None
    )
    account_label = await providers_svc.fetch_account_label(provider, token.access_token)

    async_dal, _dal_ = _dal()
    try:
        await connections_svc.upsert_connection(
            async_dal,
            payload.community_id,
            provider,
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            expires_at=expires_at,
            scopes=token.scopes,
            account_label=account_label,
            actor_user_id=payload.user_id,
        )
    except (connections_svc.UnsupportedProvider, ApiError):
        return _callback_html(provider=provider, ok=False, error="save_failed")

    return _callback_html(provider=provider, ok=True)


# ===== Admin: delete =====


@connections_bp.route("/communities/<int:community_id>/connections/<provider>", methods=["DELETE"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("community.connections:write")  # type: ignore[untyped-decorator]
async def delete_connection(community_id: int, provider: str) -> tuple[Any, int]:
    """`DELETE /api/v1/communities/<id>/connections/<provider>`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    if not await feature_enabled(FEATURE_COMMUNITY_CONNECTIONS, tenant=ctx.tenant_slug):
        return _err(payment_required("Community connections require a Professional plan or higher"))
    if not _tenant_ok(community_id):
        return _err(not_found("Community not found"))

    async_dal, _dal_ = _dal()
    user_id = get_current_user_id(request)
    try:
        deleted = await connections_svc.delete_connection(
            async_dal, community_id, provider, user_id
        )
    except connections_svc.UnsupportedProvider:
        return _err(bad_request(f"Unsupported provider: {provider}"))

    if not deleted:
        return _err(not_found("No active connection for this provider"))
    return "", 204


# ===== Internal (service-to-service, X-Service-Key) =====


@connections_internal_bp.route(
    "/communities/<int:community_id>/connections/<provider>/token", methods=["GET"]
)
async def internal_get_token(community_id: int, provider: str) -> tuple[dict[str, Any], int]:
    """`GET /api/v1/internal/communities/<id>/connections/<provider>/token`.

    Transparently refreshes and persists a token within `_REFRESH_SKEW_S`
    of expiry (or already past it) before returning it, so callers never
    have to implement their own refresh-then-retry loop. Never logs a
    token value on any path.
    """
    if not is_valid_service_key(request):
        return {"success": False, "error": "Invalid service key"}, 401

    async_dal, dal = _dal()
    if not _community_exists(dal, community_id):
        return _err(not_found("Community not found"))

    try:
        tokens = await connections_svc.get_decrypted_tokens(async_dal, community_id, provider)
    except connections_svc.UnsupportedProvider:
        return _err(bad_request(f"Unsupported provider: {provider}"))

    if tokens is None:
        return _err(not_found("No active connection for this provider"))

    needs_refresh = (
        tokens.refresh_token is not None
        and tokens.expires_at is not None
        and tokens.expires_at <= datetime.now(UTC) + timedelta(seconds=_REFRESH_SKEW_S)
    )
    if not needs_refresh:
        return _envelope(
            {
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "expires_at": tokens.expires_at.isoformat() if tokens.expires_at else None,
                "scopes": tokens.scopes,
            }
        )

    assert tokens.refresh_token is not None  # nosec B101 - guarded by needs_refresh above
    try:
        refreshed = await providers_svc.refresh_access_token(
            provider, refresh_token=tokens.refresh_token
        )
    except (ValueError, providers_svc.ProviderNotConfigured, providers_svc.OAuthExchangeError):
        return {"error": "refresh_failed"}, 502

    new_expires_at = (
        datetime.now(UTC) + timedelta(seconds=refreshed.expires_in)
        if refreshed.expires_in is not None
        else None
    )
    await connections_svc.store_refreshed_access_token(
        async_dal,
        community_id,
        provider,
        access_token=refreshed.access_token,
        expires_at=new_expires_at,
    )
    return _envelope(
        {
            "access_token": refreshed.access_token,
            "refresh_token": refreshed.refresh_token or tokens.refresh_token,
            "expires_at": new_expires_at.isoformat() if new_expires_at else None,
            "scopes": refreshed.scopes or tokens.scopes,
        }
    )


BLUEPRINTS: list[Blueprint] = [connections_bp, connections_internal_bp]
