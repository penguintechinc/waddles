"""v1 YouTube connection routes: the OAuth connect flow and live-chat discovery.

The generic connection CRUD (``blueprints/v1/provider_connections.py``) already serves the
``youtube`` provider -- list, read, ``PATCH`` (toggle, rotate credentials, and the MANUAL
``live_chat_id`` binding), ``DELETE`` and the data-plane list/resolve. This module adds the three
routes that CRUD cannot express:

* ``POST /api/v1/communities/<id>/provider-connections/youtube/authorize`` -- admin. Starts the
  Google OAuth (authorization code + PKCE) flow and returns the consent URL.
* ``GET  /api/v1/provider-connections/youtube/callback`` -- PUBLIC (Google's redirect target; the
  browser carries no bearer token, so the single-use ``state`` is the only binding back to a
  tenant/community/admin and nothing tenant-shaped is read from the query).
* ``POST /api/v1/communities/<id>/provider-connections/<conn_id>/discover`` -- admin. Looks up the
  channel's active live broadcast and WRITES its ``live_chat_id`` into the binding metadata.

Both admin routes run ``tenant_middleware`` -> ``require_scope("connections:admin")`` ->
``authorize_community`` (DB-backed: the caller administers THIS community of THEIR tenant) -> the
two-gate feature check, exactly like the Discord/Twitch writes. Responses are explicit DTOs with
``@validate_response``; no token, ciphertext or client secret has a field to travel in.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final

from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from pydantic import ConfigDict
from quart import Blueprint, Response, current_app, request
from quart_schema import validate_request, validate_response

from blueprints.v1.provider_connections import (
    SCOPE_CONNECTIONS_ADMIN,
    ProviderConnectionResponse,
    _admin_guard,
    _connection_dto,
    _guarded,
    _service,
)
from services import youtube_install as install
from services.errors import ApiError, bad_request, forbidden
from services.provider_connections.youtube import PROVIDER_ID

logger = logging.getLogger(__name__)

youtube_connections_bp = Blueprint("v1_youtube_connections", __name__, url_prefix="/api/v1")

_BASE: Final = "/communities/<int:community_id>/provider-connections"
_TOKEN_FIELD_RE: Final = re.compile(r"[\x21-\x7e]+")


@dataclass(slots=True, frozen=True)
class YoutubeAuthorizeRequest:
    """Body of ``authorize``: the tenant's OWN Google OAuth application, or nothing.

    Both fields together (a non-global tenant bringing its own application) or neither (the global
    tenant's platform application, or one this tenant stored in an earlier install). The secret is
    write-only: sealed into the single-use state and never echoed.
    """

    __pydantic_config__ = ConfigDict(extra="forbid")

    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)


@dataclass(slots=True, frozen=True)
class YoutubeAuthorizeResponse:
    """Response of ``authorize``: where the admin's browser goes next."""

    success: bool
    authorizeUrl: str  # noqa: N815 - camelCase is the wire shape of every connection DTO


def _client_field(value: str | None, name: str) -> str | None:
    """Shape-check a client id/secret (printable ASCII, bounded); the value is never echoed."""
    if value is None:
        return None
    if (
        not value
        or len(value) > install.MAX_CLIENT_FIELD_LENGTH
        or not _TOKEN_FIELD_RE.fullmatch(value)
    ):
        raise bad_request(f"{name} is invalid")
    return value


@youtube_connections_bp.route(_BASE + "/youtube/authorize", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_CONNECTIONS_ADMIN)  # type: ignore[misc]
@validate_request(YoutubeAuthorizeRequest)
@validate_response(YoutubeAuthorizeResponse)
@_guarded("youtube_authorize")
async def authorize_youtube(
    community_id: int, data: YoutubeAuthorizeRequest
) -> YoutubeAuthorizeResponse:
    """``POST`` -- mint state + PKCE and return Google's consent URL for this community."""
    tenant_id, actor = await _admin_guard(community_id)
    ctx = get_tenant_context(request)
    if ctx is None or actor is None:
        raise forbidden("Tenant context not resolved")
    client_id = _client_field(data.client_id, "client_id")
    client_secret = _client_field(data.client_secret, "client_secret")
    try:
        url = await install.begin_install(
            current_app.config["dal"],
            tenant_id=tenant_id,
            is_default_tenant=ctx.is_default,
            community_id=community_id,
            user_id=actor,
            client_id=client_id,
            client_secret=client_secret,
        )
    except install.YoutubeInstallError as exc:
        raise ApiError(
            "This tenant has no usable YouTube OAuth application; supply client_id and "
            "client_secret (the global tenant cannot).",
            422,
            "YOUTUBE_APP_NOT_CONFIGURED",
        ) from exc
    logger.info("youtube_connections.authorize_started community_id=%d", community_id)
    return YoutubeAuthorizeResponse(success=True, authorizeUrl=url)


@youtube_connections_bp.route(_BASE + "/<int:connection_id>/discover", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_CONNECTIONS_ADMIN)  # type: ignore[misc]
@validate_response(ProviderConnectionResponse)
@_guarded("discover")
async def discover_live_chat(community_id: int, connection_id: int) -> ProviderConnectionResponse:
    """``POST`` -- find the channel's active live chat and store it as the binding.

    One YouTube call (1 quota unit), at most one per connection per cooldown window. A provider
    without discovery answers 400; "no live broadcast right now" is 422 and writes nothing.
    """
    tenant_id, _actor = await _admin_guard(community_id)
    service = _service()
    await service.get(tenant_id=tenant_id, community_id=community_id, connection_id=connection_id)
    if not await install.acquire_discover_slot(tenant_id=tenant_id, connection_id=connection_id):
        raise ApiError(
            "Live chat discovery was just run for this connection; wait a moment.",
            429,
            "TOO_MANY_REQUESTS",
        )
    view = await service.discover_metadata(
        tenant_id=tenant_id, community_id=community_id, connection_id=connection_id
    )
    return ProviderConnectionResponse(success=True, connection=_connection_dto(view))


def _callback_html(*, ok: bool, error: str | None = None) -> Response:
    """Popup-closing page: posts ``{type, ok, error?}`` to ``window.opener`` (this origin only).

    NEVER embeds a token, code, state, client id or secret -- ``error`` is one of the install
    module's fixed ``ERR_*`` constants.
    """
    message: dict[str, Any] = {"type": "YOUTUBE_INSTALL_CALLBACK", "ok": ok}
    if error is not None:
        message["error"] = error
    body = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Connecting YouTube...</title></head>
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
        headers={
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


@youtube_connections_bp.route("/provider-connections/youtube/callback", methods=["GET"])
async def youtube_callback() -> tuple[Response, int] | Response:
    """``GET`` -- PUBLIC, Google's redirect target; finishes the install from the state alone."""
    try:
        await install.complete_install(
            current_app.config["dal"],
            _service(),
            state=request.args.get("state", ""),
            code=request.args.get("code"),
            provider_error=request.args.get("error"),
        )
    except install.YoutubeInstallError as exc:
        logger.info("youtube_connections.callback_failed code=%s", exc.code)
        status = 400 if exc.code in (install.ERR_STATE, install.ERR_NO_CODE) else 200
        return _callback_html(ok=False, error=exc.code), status
    return _callback_html(ok=True)


BLUEPRINTS = [youtube_connections_bp]

__all__ = ["BLUEPRINTS", "PROVIDER_ID", "youtube_connections_bp"]
