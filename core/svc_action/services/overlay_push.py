"""Push a payload to the Rust svc-presentation router (`POST /{overlay_code}/{surface}/push`).

The single place svc-action talks to svc-presentation. It exists because the router's real
contract differs from what the first adapter assumed: the URL carries the community's unguessable
`overlay_code` (never its integer id) and the caller must present a hub-api-minted PUSH machine
JWT scoped to exactly that community (`presentation.overlay:push:<community_id>`), not a static
bearer secret.

Both inputs are resolved server-side and never taken from user input: the overlay code is read
from `communities.overlay_code` for the envelope's own community, and the JWT is minted by hub-api
from this pod's projected ServiceAccount token. Anything unresolvable raises `OverlayPushError`
-- there is no silent fallback to the retired `/overlay/{id}/...` route.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

import httpx
from waddle_transports.service_jwt_client import ServiceTokenError, mint_service_token

logger = logging.getLogger(__name__)

#: Scope base hub-api's `ServiceAuthService/MintServiceToken` expects for an overlay push.
_PUSH_SCOPE_BASE = "presentation.overlay:push"
_DEFAULT_SA_TOKEN_PATH = "/var/run/secrets/waddlebot/service-jwt/service-jwt-bootstrap-token"  # noqa: S105 -- path, not a secret
#: Shape pinned by alembic 0057's CHECK constraint (16 lowercase hex = 64-bit CSPRNG value).
_OVERLAY_CODE_RE = re.compile(r"[0-9a-f]{16}")
#: Re-mint this many seconds before the JWT's own expiry.
_JWT_SKEW_S = 30.0
_PUSH_TIMEOUT_S = 5.0

#: `community_id -> (monotonic_expiry, jwt)`.
_jwt_cache: dict[int, tuple[float, str]] = {}


class OverlayPushError(Exception):
    """The overlay push could not be resolved, authorized, or delivered (always loud)."""


def reset_cache_for_tests() -> None:
    """Drop every cached PUSH JWT (test isolation only)."""
    _jwt_cache.clear()


def _require_env(name: str) -> str:
    """Return a non-empty env var or raise -- an unset deployment value is a wiring bug."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise OverlayPushError(f"{name} is not set; cannot push to svc-presentation")
    return value


async def resolve_overlay_code(dal: Any, community_id: int) -> str:
    """Read `communities.overlay_code` for `community_id`; raise if absent or malformed.

    `dal` is svc-action's reflected `AsyncDAL`. The id comes from the stage envelope, so the
    code is always the pushing community's own -- a push can never be steered at another one.
    """
    table = dal.dal.communities
    rows = await dal.select_async(dal.dal(table.id == community_id), limitby=(0, 1))
    if not rows:
        raise OverlayPushError(f"community {community_id} not found; no overlay code")
    code = getattr(rows[0], "overlay_code", None)
    if not isinstance(code, str) or not _OVERLAY_CODE_RE.fullmatch(code):
        raise OverlayPushError(
            f"community {community_id} has no valid overlay_code (alembic 0057 applied?)"
        )
    return code


async def mint_push_token(community_id: int) -> str:
    """Return a community-bound PUSH JWT from hub-api, cached until shortly before expiry.

    Mints over gRPC (`ServiceAuthService/MintServiceToken`, `SERVICE_JWT_TOKEN_ENDPOINT` = hub-api's
    internal gRPC endpoint), bootstrapping with the pod's audience-scoped projected ServiceAccount
    token (no static secret). Every failure raises `OverlayPushError` carrying the reason only,
    never a token.
    """
    cached = _jwt_cache.get(community_id)
    if cached is not None and time.monotonic() < cached[0]:
        return cached[1]

    endpoint = _require_env("SERVICE_JWT_TOKEN_ENDPOINT")
    sa_path = os.environ.get("SERVICE_JWT_SA_TOKEN_PATH", "").strip() or _DEFAULT_SA_TOKEN_PATH
    try:
        minted = await mint_service_token(
            endpoint=endpoint,
            scope=_PUSH_SCOPE_BASE,
            community_id=community_id,
            sa_token_path=sa_path,
            timeout_s=_PUSH_TIMEOUT_S,
        )
    except ServiceTokenError as exc:
        raise OverlayPushError(f"hub-api PUSH token mint failed: {exc}") from exc
    _jwt_cache[community_id] = (
        time.monotonic() + max(minted.expires_in - _JWT_SKEW_S, 0.0),
        minted.token,
    )
    return minted.token


async def push_overlay(
    http_client: httpx.AsyncClient,
    dal: Any,
    *,
    community_id: int,
    surface: str,
    body: dict[str, Any],
) -> int:
    """POST `body` to `{PRESENTATION_URL}/{overlay_code}/{surface}/push`; return the HTTP status.

    Resolves the overlay code and mints the PUSH JWT for `community_id` first. Raises
    `OverlayPushError` for an unset URL, unresolvable code, token failure, transport error, or
    any non-2xx answer (a 401/403 also evicts the cached JWT so the next attempt re-mints).
    """
    base = _require_env("PRESENTATION_URL").rstrip("/")
    code = await resolve_overlay_code(dal, community_id)
    token = await mint_push_token(community_id)
    # The code is a path handle only; it is deliberately kept out of every log line below.
    url = f"{base}/{code}/{surface}/push"
    try:
        response = await http_client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=_PUSH_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        raise OverlayPushError(f"svc-presentation unreachable: {type(exc).__name__}") from exc
    if response.status_code in (401, 403):
        _jwt_cache.pop(community_id, None)
    if not 200 <= response.status_code < 300:
        raise OverlayPushError(
            f"svc-presentation rejected the {surface} push: HTTP {response.status_code}"
        )
    logger.debug(
        "overlay_push.sent community_id=%s surface=%s status=%s",
        community_id,
        surface,
        response.status_code,
    )
    return response.status_code
