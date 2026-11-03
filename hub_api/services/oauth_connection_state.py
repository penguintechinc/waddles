"""Single-use OAuth `state` token store for Community Connections (gh-320).

The authorize route (`blueprints/v1/community_connections.py`) mints an
unguessable `state` token and stashes the request context it needs to
finish the flow (which community/provider/user initiated it, the exact
`redirect_uri` sent to the provider, and a PKCE `code_verifier` for
providers that use it) server-side, keyed by that token. The public
callback route later exchanges the `state` query param back for that
context and MUST consume it exactly once -- a state token that could be
replayed would let an attacker who observes/guesses one redirect a
victim's authorization code into the attacker's own community.

Backend: Redis/Valkey (`RATE_LIMITER_CONFIG_KEY`'s existing connection,
reused directly the same way `blueprints/v1/community_music_queue.py`'s
own `_redis_client()` does -- see that function's docstring for why
reaching into `RateLimiter`'s private `_redis` attribute is preferable to
opening a second connection -- falling back to a lazily-opened client
against `HubAPIConfig.valkey_url` for apps that never installed rate
limiting, e.g. most test apps). Redis over the self-contained
AES-GCM-encrypted-token alternative this module could otherwise use
(`services/platform_integrations_crypto.py`'s `CREDENTIAL_ENCRYPTION_KEY`
primitive) because hub-api runs multiple replicas in beta/gamma/prod --
an in-process single-use set would not be visible to whichever pod the
OAuth provider's callback redirect happens to land on, silently breaking
the flow under any replica count above 1. Redis's `GETDEL` (atomic
get-and-delete, Redis/Valkey >=6.2) gives single-use consumption without
a separate DEL round trip or a race between two concurrent callback
requests both reading the same state before either deletes it.

The state token itself (`secrets.token_urlsafe(32)`, ~256 bits of
entropy) is the redis key suffix -- unguessable, so no additional
encryption of the stored payload is needed: the payload never leaves
hub-api's own infra (redis), only the opaque token travels through the
browser/provider redirect chain.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any, cast

from quart import current_app

from config import HubAPIConfig
from services.rate_limiting import RATE_LIMITER_CONFIG_KEY

logger = logging.getLogger(__name__)

#: Redis key prefix every state token is stored under.
_STATE_KEY_PREFIX = "oauth:conn:state:"

#: `app.config` key a lazily-opened raw Valkey client is cached under, for
#: apps that never called `services.rate_limiting.install_rate_limiting()`
#: -- mirrors `blueprints/v1/community_music_queue.py`'s
#: `MUSIC_PLAYBACK_REDIS_CONFIG_KEY` precedent exactly.
_CONNECTIONS_REDIS_CONFIG_KEY = "connections_oauth_state_redis"


@dataclass(slots=True, frozen=True)
class StatePayload:
    """Decoded, single-use context a `state` token was minted to carry."""

    community_id: int
    provider: str
    user_id: int
    redirect_uri: str
    code_verifier: str | None
    #: The tenant validated when the flow STARTED (authorize route); the public callback has no
    #: JWT, so this server-side value is what the DB-side tenant check receives.
    tenant_id: int | None = None


def _redis_client() -> Any:
    """Resolve the async Valkey/Redis client used for `oauth:conn:state:*` tokens.

    Identical resolution order to `blueprints/v1/community_music_queue.py`'s
    `_redis_client()` -- see that function's own docstring for the full
    rationale. Reuses `RateLimiter`'s already-open connection when the app
    installed rate limiting (every real deployment, via `app.py`); lazily
    opens and caches its own client against `HubAPIConfig.valkey_url`
    otherwise (most test apps).
    """
    limiter = current_app.config.get(RATE_LIMITER_CONFIG_KEY)
    existing = getattr(limiter, "_redis", None) if limiter is not None else None
    if existing is not None:
        return existing

    cached = current_app.config.get(_CONNECTIONS_REDIS_CONFIG_KEY)
    if cached is not None:
        return cached

    import redis.asyncio as redis_asyncio

    cfg = cast(HubAPIConfig, current_app.config["HUB_API_CONFIG"])
    client = redis_asyncio.from_url(
        cfg.valkey_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    current_app.config[_CONNECTIONS_REDIS_CONFIG_KEY] = client
    return client


def _ttl_seconds() -> int:
    cfg = cast(HubAPIConfig, current_app.config["HUB_API_CONFIG"])
    return cfg.connections_state_ttl_s


async def create_state(
    *,
    community_id: int,
    provider: str,
    user_id: int,
    redirect_uri: str,
    code_verifier: str | None,
    tenant_id: int | None = None,
) -> str:
    """Mint a fresh single-use `state` token and store its context in Redis.

    Returns the opaque token to embed in the provider's `authorize` URL --
    never the payload itself.
    """
    token = secrets.token_urlsafe(32)
    payload = {
        "community_id": community_id,
        "provider": provider,
        "user_id": user_id,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
        "tenant_id": tenant_id,
    }
    client = _redis_client()
    await client.set(f"{_STATE_KEY_PREFIX}{token}", json.dumps(payload), ex=_ttl_seconds())
    return token


async def consume_state(state: str) -> StatePayload | None:
    """Atomically fetch-and-delete `state`'s context; `None` if absent/expired/already used.

    `GETDEL` makes this single-use: a second call with the same token
    (whether a legitimate retry or a replay attempt) always misses, even
    under concurrent requests, since the delete is part of the same atomic
    command as the read.
    """
    if not state:
        return None
    client = _redis_client()
    try:
        raw = await client.getdel(f"{_STATE_KEY_PREFIX}{state}")
    except Exception:  # noqa: BLE001 - a Redis hiccup must fail the flow, not crash it
        logger.exception("oauth_connection_state.consume_failed")
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return StatePayload(
            community_id=int(data["community_id"]),
            provider=str(data["provider"]),
            user_id=int(data["user_id"]),
            redirect_uri=str(data["redirect_uri"]),
            code_verifier=data.get("code_verifier"),
            tenant_id=int(data["tenant_id"]) if data.get("tenant_id") is not None else None,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        logger.warning("oauth_connection_state.malformed_payload")
        return None
