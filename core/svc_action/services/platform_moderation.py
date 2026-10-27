"""services/platform_moderation.py -- Discord/Twitch REST/relay clients for moderation ENFORCEMENT.

Split out from `builtin_handlers/moderation_enforce_action.py` so the two
platforms' actual call logic (SSRF-guarded Discord REST + Valkey-relayed
Twitch IRC) is independently testable and the action-stage entrypoint
itself stays a thin per-platform dispatcher -- mirrors this repo's
`builtin_handlers/social_alias_action.py` split into `_send_discord`/`_send_twitch`,
except these two platform implementations (SSRF-guarded REST call +
inline 429 retry-once, or relay-queue warn + Helix ban) are large enough
to earn a dedicated module rather than two private handler functions.

Every function here raises `waddle_transports.{Retryable,NonRetryable}
TransportError` with a SPECIFIC message on every failure path (design
doc's "error states must be specific, e.g. 'oauth token didn't work'")
-- never a bare/unclassified exception -- and returns an
:class:`EnforcementOutcome` on success. `builtin_handlers/moderation_enforce_
action.py::enforce()` owns config parsing, target resolution, and
combining outcomes into one `TransportResult`; this module owns nothing
but "make the actual call, classify the actual response."

`resolve_community_moderator_token()` (gh-320) is this module's
community-aware addition: `builtin_handlers/moderation_enforce_action.py`'s Twitch
timeout path (the one call here needing a *user*-scoped token, `twitch_
timeout`'s `moderator_token`) tries it FIRST, falling back to the
existing `moderator_token_ref`/`resolve_secret` config path only when no
per-community Twitch connection exists -- see that function's own
docstring.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from waddle_transports import NonRetryableTransportError, RetryableTransportError
from waddle_transports.transports.irc_relay import RelayOutboundIrcTransport, RelayRedisLike
from waddle_transports.url_guard import SSRFError, guarded_request

logger = logging.getLogger(__name__)

try:
    from waddle_transports.community_credentials import resolve_community_tokens
except ImportError:  # pragma: no cover -- exercised only before gh-320's resolver lands
    resolve_community_tokens = None

#: Discord's own ceiling for `communication_disabled_until` (28 days out
#: from now) -- a request beyond this is rejected by Discord itself with
#: a 400, so this handler clamps proactively rather than letting that
#: round-trip happen.
DISCORD_MAX_TIMEOUT_SECONDS = 28 * 24 * 3600  # 2,419,200

#: Twitch Helix `/moderation/bans` max timed-ban `duration` (14 days) --
#: a duration beyond this is a permanent ban (omit `duration` entirely),
#: out of scope for this enforcement handler (timeout only, never a
#: permanent ban).
TWITCH_MAX_TIMEOUT_SECONDS = 1_209_600

#: Real API roots -- public (no leading underscore) since `builtin_handlers/
#: moderation_enforce_action.py` reuses them as the `config["api_base"]`
#: fallback default, mirroring `discord_send_action.py`'s own
#: `config.get("api_base", _DEFAULT_API_BASE)` convention.
DEFAULT_DISCORD_API_BASE = "https://discord.com/api/v10"
DEFAULT_TWITCH_API_BASE = "https://api.twitch.tv/helix"

#: Upper bound on the inline sleep this module performs for a Discord 429
#: `Retry-After` -- protects the dispatch loop from an unbounded block on
#: a misbehaving/malicious `Retry-After` value; a wait longer than this is
#: better served by `runner.py`'s own platform-level `retry_with_backoff`
#: on a `RetryableTransportError` from the exhausted-retry branch below.
_MAX_INLINE_RETRY_AFTER_SECONDS = 5.0


@dataclass(slots=True, frozen=True)
class EnforcementOutcome:
    """One applied enforcement step (a warn post or a timeout/ban call)."""

    action: str  # "warn" | "timeout"
    detail: str
    http_status: int | None = None


def clamp_timeout_seconds(timeout_s: int, *, max_seconds: int) -> int:
    """Clamp a positive `timeout_s` to `max_seconds`; non-positive values pass through unchanged."""
    if timeout_s <= 0:
        return timeout_s
    return min(timeout_s, max_seconds)


def _parse_retry_after(raw: str | None) -> float:
    """Parse a Discord `Retry-After` header value to seconds, capped and fail-safe.

    An absent or unparseable value falls back to `1.0` (Discord's own
    typical minimum); any value is capped at `_MAX_INLINE_RETRY_AFTER_
    SECONDS` regardless of what the header claims.
    """
    try:
        seconds = float(raw) if raw is not None else 1.0
    except ValueError:
        seconds = 1.0
    return max(0.0, min(seconds, _MAX_INLINE_RETRY_AFTER_SECONDS))


def _classify_discord_response(response: httpx.Response) -> None:
    """Raise a SPECIFIC typed error for a non-2xx Discord response; return `None` on 2xx."""
    if response.status_code == 401:
        raise NonRetryableTransportError("discord bot token didn't work (401)", http_status=401)
    if response.status_code == 403:
        raise NonRetryableTransportError(
            "bot lacks MODERATE_MEMBERS permission (403)", http_status=403
        )
    if 400 <= response.status_code < 500:
        raise NonRetryableTransportError(
            f"discord API returned client error: HTTP {response.status_code} "
            f"{response.text[:200]}",
            http_status=response.status_code,
        )
    if response.status_code >= 500:
        raise RetryableTransportError(
            f"discord API server error: HTTP {response.status_code}",
            http_status=response.status_code,
        )


async def _discord_request(
    http_client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json: dict[str, object],
) -> httpx.Response:
    """SSRF-guarded Discord call with a one-shot inline 429 retry (gh-304 spec).

    Divergence from `discord_send_action.py`'s own 429 handling (which
    always defers to `runner.py`'s platform-level `retry_with_backoff`,
    re-dispatching the whole envelope): moderation enforcement combines a
    warn AND a timeout into one dispatch, so a transient rate limit on
    the FIRST call would otherwise force a full second dispatch (and a
    duplicate warn message) just to retry the second. Respecting `Retry-
    After` inline once here avoids that duplication; a 429 that persists
    past the one retry still becomes a `RetryableTransportError` so the
    platform-level retry loop remains the backstop.
    """
    try:
        response: httpx.Response = await guarded_request(
            http_client, method, url, headers=headers, json=json
        )
    except SSRFError as exc:
        raise NonRetryableTransportError(
            f"discord API URL rejected by SSRF guard: {exc}"
        ) from exc
    except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
        raise RetryableTransportError(f"discord API request failed: {exc}") from exc

    if response.status_code == 429:
        await asyncio.sleep(_parse_retry_after(response.headers.get("Retry-After")))
        try:
            response = await guarded_request(http_client, method, url, headers=headers, json=json)
        except SSRFError as exc:
            raise NonRetryableTransportError(
                f"discord API URL rejected by SSRF guard: {exc}"
            ) from exc
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
            raise RetryableTransportError(f"discord API request failed: {exc}") from exc
        if response.status_code == 429:
            raise RetryableTransportError(
                "discord API rate limited (429), retry exhausted", http_status=429
            )

    _classify_discord_response(response)
    return response


async def discord_warn(
    http_client: httpx.AsyncClient,
    *,
    channel_id: str,
    text: str,
    bot_token: str,
    api_base: str = DEFAULT_DISCORD_API_BASE,
) -> EnforcementOutcome:
    """Post `text` to `channel_id` -- same Create Message call `discord_send_action.py` makes."""
    url = f"{api_base}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
    response = await _discord_request(
        http_client, "POST", url, headers=headers, json={"content": text}
    )
    return EnforcementOutcome(
        action="warn",
        detail=f"warn posted, channel={channel_id}",
        http_status=response.status_code,
    )


async def discord_timeout(
    http_client: httpx.AsyncClient,
    *,
    guild_id: str,
    user_id: str,
    timeout_s: int,
    reason: str,
    bot_token: str,
    api_base: str = DEFAULT_DISCORD_API_BASE,
) -> EnforcementOutcome:
    """`PATCH /guilds/{guild_id}/members/{user_id}` -- sets `communication_disabled_until`.

    `timeout_s` is clamped to :data:`DISCORD_MAX_TIMEOUT_SECONDS` before
    being applied. `reason` is sent as `X-Audit-Log-Reason` (the matched
    moderation category), visible in the guild's own audit log.
    """
    clamped = clamp_timeout_seconds(timeout_s, max_seconds=DISCORD_MAX_TIMEOUT_SECONDS)
    until = (datetime.now(UTC) + timedelta(seconds=clamped)).isoformat().replace("+00:00", "Z")
    url = f"{api_base}/guilds/{guild_id}/members/{user_id}"
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
        "X-Audit-Log-Reason": reason,
    }
    response = await _discord_request(
        http_client,
        "PATCH",
        url,
        headers=headers,
        json={"communication_disabled_until": until},
    )
    return EnforcementOutcome(
        action="timeout",
        detail=f"timed out {clamped}s, guild={guild_id} user={user_id}",
        http_status=response.status_code,
    )


def _classify_twitch_response(response: httpx.Response) -> None:
    """Raise a SPECIFIC typed error for a non-2xx Twitch response; return `None` on 2xx."""
    if response.status_code == 401:
        raise NonRetryableTransportError("twitch oauth token didn't work (401)", http_status=401)
    if response.status_code == 403:
        raise NonRetryableTransportError(
            "twitch moderator token lacks moderator:manage:banned_users scope (403)",
            http_status=403,
        )
    if response.status_code == 429:
        raise RetryableTransportError("twitch API rate limited (429)", http_status=429)
    if 400 <= response.status_code < 500:
        raise NonRetryableTransportError(
            f"twitch API returned client error: HTTP {response.status_code} "
            f"{response.text[:200]}",
            http_status=response.status_code,
        )
    if response.status_code >= 500:
        raise RetryableTransportError(
            f"twitch API server error: HTTP {response.status_code}",
            http_status=response.status_code,
        )


async def resolve_community_moderator_token(community_id: int | None) -> str | None:
    """Best-effort per-community Twitch USER token via the shared community-credentials resolver.

    Returns the community's connected Twitch access token (gh-320) if one
    exists (`resolve_community_tokens(community_id, "twitch")` returns
    `source == "community"`); `None` for every other outcome -- no
    community connection, `source == "env"`, the resolver module not
    importable yet, or the resolver itself raising. Never raises: the
    caller (`builtin_handlers/moderation_enforce_action.py::_enforce_twitch`) falls
    back to its existing `moderator_token_ref`/`resolve_secret` config
    path whenever this returns `None`, so a community with no connected
    Twitch account behaves exactly as before this feature existed.

    Deliberately does not attempt token *validation* (scope check) here --
    same as `twitch_timeout`'s own docstring, an invalid/under-scoped
    token surfaces as a 401/403 from the real Helix call, classified by
    `_classify_twitch_response` same as any other configured token.
    """
    if resolve_community_tokens is None:
        return None
    try:
        tokens = await resolve_community_tokens(community_id, "twitch")
    except Exception as exc:  # noqa: BLE001 -- a resolver failure must never block enforcement
        logger.debug(
            "platform_moderation.community_resolve_failed community_id=%s error=%s",
            community_id,
            type(exc).__name__,
        )
        return None

    if tokens is not None and tokens.source == "community" and tokens.access_token:
        logger.debug(
            "platform_moderation.token_source=community community_id=%s provider=twitch",
            community_id,
        )
        return str(tokens.access_token)

    logger.debug(
        "platform_moderation.token_source=env community_id=%s provider=twitch", community_id
    )
    return None


async def twitch_timeout(
    http_client: httpx.AsyncClient,
    *,
    broadcaster_id: str,
    moderator_id: str,
    user_id: str,
    timeout_s: int,
    reason: str,
    moderator_token: str,
    client_id: str,
    api_base: str = DEFAULT_TWITCH_API_BASE,
) -> EnforcementOutcome:
    """Helix `POST /moderation/bans` -- a timed ban (Twitch's own equivalent of a timeout).

    Requires a USER access token (`moderator_token`, scope `moderator:
    manage:banned_users`) belonging to the broadcaster or one of their
    mods -- an app/client-credentials token can never authorize this
    endpoint (Twitch rejects it with 401/403 same as any other invalid
    token); the caller (`builtin_handlers/moderation_enforce_action.py`) is
    responsible for refusing BEFORE calling this function when no such
    token is configured at all (see that module's own specific "requires
    a user token" error), so a 401/403 actually reaching this function
    means the configured moderator token itself is invalid/expired/
    under-scoped, not that one was never supplied.
    """
    clamped = clamp_timeout_seconds(timeout_s, max_seconds=TWITCH_MAX_TIMEOUT_SECONDS)
    query = urlencode({"broadcaster_id": broadcaster_id, "moderator_id": moderator_id})
    url = f"{api_base}/moderation/bans?{query}"
    headers = {
        "Authorization": f"Bearer {moderator_token}",
        "Client-Id": client_id,
        "Content-Type": "application/json",
    }
    body = {"data": {"user_id": user_id, "duration": clamped, "reason": reason[:500]}}

    try:
        response = await guarded_request(http_client, "POST", url, headers=headers, json=body)
    except SSRFError as exc:
        raise NonRetryableTransportError(f"twitch API URL rejected by SSRF guard: {exc}") from exc
    except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
        raise RetryableTransportError(f"twitch API request failed: {exc}") from exc

    _classify_twitch_response(response)
    return EnforcementOutcome(
        action="timeout",
        detail=f"timed out {clamped}s, broadcaster={broadcaster_id} user={user_id}",
        http_status=response.status_code,
    )


async def twitch_warn(
    *,
    channel: str,
    text: str,
    redis_client: RelayRedisLike,
) -> EnforcementOutcome:
    """Relay `text` to `channel` via the existing outbound IRC relay path (`twitch_send_action.py`).

    Never calls a REST API directly -- svc-action holds no Twitch chat
    credentials; the warn is LPUSHed onto the same Valkey relay queue
    `outbound_drain.py` drains through the one real IRC socket svc-ingest
    already holds, exactly like a normal chat reply.
    """
    transport = RelayOutboundIrcTransport(provider="twitch", redis_client=redis_client)
    result = await transport.send({"channel": channel}, {"text": text})
    return EnforcementOutcome(action="warn", detail=result.detail)
