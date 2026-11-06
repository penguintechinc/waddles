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

import asyncio
import logging
import os
import re
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: Request body field + scope base hub-api's `POST /internal/service-token` expects.
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


def _read_text(path: str) -> str:
    """Read a small file (run via `asyncio.to_thread` so the loop never blocks on disk)."""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


async def mint_push_token(http_client: httpx.AsyncClient, community_id: int) -> str:
    """Return a community-bound PUSH JWT from hub-api, cached until shortly before expiry.

    Bootstraps with the pod's audience-scoped projected ServiceAccount token (no static
    secret). Every failure raises `OverlayPushError` carrying ids/status only, never a token.
    """
    cached = _jwt_cache.get(community_id)
    if cached is not None and time.monotonic() < cached[0]:
        return cached[1]

    endpoint = _require_env("SERVICE_JWT_TOKEN_ENDPOINT")
    sa_path = os.environ.get("SERVICE_JWT_SA_TOKEN_PATH", "").strip() or _DEFAULT_SA_TOKEN_PATH
    try:
        sa_token = (await asyncio.to_thread(_read_text, sa_path)).strip()
    except OSError as exc:
        raise OverlayPushError(
            f"projected ServiceAccount token unreadable: {type(exc).__name__}"
        ) from exc
    if not sa_token:
        raise OverlayPushError("projected ServiceAccount token is empty")

    try:
        response = await http_client.post(
            endpoint,
            headers={"Authorization": f"Bearer {sa_token}"},
            json={"scope": _PUSH_SCOPE_BASE, "community_id": community_id},
            timeout=_PUSH_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        raise OverlayPushError(f"hub-api token endpoint unreachable: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise OverlayPushError(
            f"hub-api refused the PUSH token request: HTTP {response.status_code}"
        )
    try:
        body = response.json()
        token = body["token"]
        expires_in = float(body["expires_in"])
    except (ValueError, KeyError, TypeError) as exc:
        raise OverlayPushError("hub-api PUSH token response malformed") from exc
    if not isinstance(token, str) or not token or expires_in <= 0:
        raise OverlayPushError("hub-api PUSH token response malformed")
    _jwt_cache[community_id] = (time.monotonic() + max(expires_in - _JWT_SKEW_S, 0.0), token)
    return token


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
    token = await mint_push_token(http_client, community_id)
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
