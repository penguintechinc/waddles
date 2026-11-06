"""Single-use CSRF `state` token store for the tenant Twitch bot-install flow (migration 0034).

Mirrors `services/oauth_connection_state.py`'s design exactly: a tenant
admin's `POST /api/v1/tenant/twitch-install/authorize` call mints an
unguessable `state` token and stashes the request context it needs to
finish the flow server-side, keyed by that token; the public callback
route later exchanges `state` back for that context and MUST consume it
exactly once -- a replayable state token would let an attacker who
observes/guesses one bind their own Twitch authorization into a victim
tenant's credentials row (or vice versa). Same Redis `GETDEL`
single-use-consumption rationale as `oauth_connection_state.py`'s own
docstring (multi-replica hub-api, atomic get-and-delete) -- kept as its
own module rather than extending that one because the payload shape
differs materially (see below), not because the mechanism does.

**Carries a real secret, unlike `oauth_connection_state.py`'s own
payload** (PKCE verifier only, never a credential): the tenant admin
supplies their Twitch application's `client_id`/`client_secret` at
authorize time, and the public callback has no other way to recover them
to complete the token exchange. `client_secret` is therefore AES-256-GCM
encrypted (`services.provider_connections.vault.seal_secret`, the same
primitive `services.credential_resolver.store_tenant_credentials` uses for
the row this flow ultimately writes) before being JSON-encoded into the
Redis value -- defense in depth on top of Redis never being reachable
outside hub-api's own infra.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any, cast

from quart import current_app

from config import HubAPIConfig
from services.install_state_binding import (
    ERR_STATE,
    InstallStateError,
    consume_bound,
    nonce_hash,
)
from services.platform_integrations_crypto import (
    PlatformCredentialCryptoError,
)
from services.provider_connections.vault import open_secret, seal_secret
from services.rate_limiting import RATE_LIMITER_CONFIG_KEY

logger = logging.getLogger(__name__)

#: Redis key prefix every state token is stored under.
_STATE_KEY_PREFIX = "oauth:twitch_install:state:"

#: `app.config` key a lazily-opened raw Valkey client is cached under, for
#: apps that never called `services.rate_limiting.install_rate_limiting()`
#: -- mirrors `oauth_connection_state.py`'s own `_CONNECTIONS_REDIS_CONFIG_KEY`.
_TWITCH_INSTALL_REDIS_CONFIG_KEY = "twitch_install_state_redis"

#: Fallback TTL if `HubAPIConfig` has no `connections_state_ttl_s` (e.g. a
#: minimal test config) -- matches that field's own default.
_DEFAULT_TTL_S = 600


@dataclass(slots=True, frozen=True)
class TwitchInstallStatePayload:
    """Decoded, single-use context a tenant Twitch-install `state` token was minted to carry."""

    tenant_id: int
    installed_by_user_id: int
    client_id: str
    client_secret: str
    redirect_uri: str


def _redis_client() -> Any:
    """Resolve the async Valkey/Redis client -- identical resolution order to sibling modules."""
    limiter = current_app.config.get(RATE_LIMITER_CONFIG_KEY)
    existing = getattr(limiter, "_redis", None) if limiter is not None else None
    if existing is not None:
        return existing

    cached = current_app.config.get(_TWITCH_INSTALL_REDIS_CONFIG_KEY)
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
    current_app.config[_TWITCH_INSTALL_REDIS_CONFIG_KEY] = client
    return client


def ttl_seconds() -> int:
    """State (and binding-cookie) lifetime in seconds, capped at 10 minutes."""
    return min(_ttl_seconds(), _DEFAULT_TTL_S)


def _ttl_seconds() -> int:
    cfg = cast(HubAPIConfig, current_app.config["HUB_API_CONFIG"])
    return cast(int, getattr(cfg, "connections_state_ttl_s", _DEFAULT_TTL_S))


async def create_state(
    *,
    tenant_id: int,
    installed_by_user_id: int,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    binding_nonce: str,
) -> str:
    """Mint a fresh single-use `state` token and store its context in Redis.

    Returns the opaque token to embed in Twitch's `authorize` URL -- never
    the payload itself. `client_secret` is encrypted before storage; never
    logged here or by any caller. `binding_nonce` is the initiating
    browser's cookie nonce (`services.install_state_binding.new_nonce`);
    only its SHA-256 is stored, and `consume_state` refuses any browser
    that cannot present it.
    """
    token = secrets.token_urlsafe(32)
    payload = {
        "tenant_id": tenant_id,
        "installed_by_user_id": installed_by_user_id,
        "client_id": client_id,
        "client_secret_ciphertext": seal_secret(client_secret),
        "redirect_uri": redirect_uri,
        "binding": nonce_hash(binding_nonce),
    }
    client = _redis_client()
    await client.set(f"{_STATE_KEY_PREFIX}{token}", json.dumps(payload), ex=_ttl_seconds())
    return token


async def consume_state(state: str, binding_nonce: str | None) -> TwitchInstallStatePayload:
    """Verify the browser binding, then atomically fetch-and-delete `state`'s context.

    Raises `InstallStateError`: `invalid_or_expired_state` if absent/expired/
    already used/malformed, `state_binding_mismatch` if `binding_nonce` is
    missing or not the one minted for this state (state left untouched).
    `GETDEL` makes it single-use even under concurrent requests. A decrypt
    failure on the stored `client_secret` fails closed like a malformed payload.
    """
    if not state or len(state) > 128:
        raise InstallStateError(ERR_STATE)
    try:
        raw = await consume_bound(_redis_client(), f"{_STATE_KEY_PREFIX}{state}", binding_nonce)
    except InstallStateError:
        raise
    except Exception as exc:  # noqa: BLE001 - a Redis hiccup must fail the flow, not crash it
        logger.exception("twitch_install_state.consume_failed")
        raise InstallStateError(ERR_STATE) from exc
    try:
        data = json.loads(raw)
        client_secret = open_secret(str(data["client_secret_ciphertext"]))
        return TwitchInstallStatePayload(
            tenant_id=int(data["tenant_id"]),
            installed_by_user_id=int(data["installed_by_user_id"]),
            client_id=str(data["client_id"]),
            client_secret=client_secret,
            redirect_uri=str(data["redirect_uri"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, PlatformCredentialCryptoError):
        logger.warning("twitch_install_state.malformed_payload")
        raise InstallStateError(ERR_STATE) from None
