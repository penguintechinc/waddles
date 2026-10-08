"""Single-use `state` token store for the per-tenant Discord bot-install flow (Bar Citizen).

Same shape as `services/oauth_connection_state.py`'s own opaque-token +
Redis-stashed-context design -- see that module's docstring for the full
rationale (Redis over a self-contained signed token because hub-api runs
multiple replicas, and `GETDEL` gives single-use consumption with no
separate round trip or TOCTOU race between two concurrent callbacks).
Copied rather than imported: that module's key prefix/TTL config are
private to the Community Connections OAuth lane (gh-320), a different
purpose from this tenant-admin bot-install flow, even though both resolve
the same `RateLimiter`/`HubAPIConfig.valkey_url` Redis connection.

This flow's payload differs in one load-bearing way: it carries the
tenant admin's SUBMITTED Discord app credentials (`client_secret`/
`bot_token`) across the browser round trip through Discord's consent
screen, so they can be verified (via the callback's code exchange) before
`services.credential_resolver.store_tenant_credentials()` ever persists
them. Those two fields are AES-256-GCM encrypted
(`services.platform_integrations_crypto.encrypt_token`) before being
JSON-encoded into Redis -- belt-and-suspenders alongside the opaque,
unguessable token key itself (`security.md` Token & Secret Hygiene: a
secret is encrypted at rest even in infra that never leaves this
service), and decrypted only once, inside `consume_state()`, by the
caller that's about to use them for the token exchange.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any, cast

from quart import current_app

from config import HubAPIConfig
from services.platform_integrations_crypto import (
    PlatformCredentialCryptoError,
    decrypt_value,
    encrypt_token,
)
from services.rate_limiting import RATE_LIMITER_CONFIG_KEY

logger = logging.getLogger(__name__)

#: Redis key prefix every install `state` token is stored under.
_STATE_KEY_PREFIX = "oauth:tenant_discord_install:state:"

#: `app.config` key a lazily-opened raw Valkey client is cached under, for
#: apps that never called `services.rate_limiting.install_rate_limiting()`
#: -- mirrors `services/oauth_connection_state.py`'s own precedent exactly.
_INSTALL_REDIS_CONFIG_KEY = "tenant_discord_install_state_redis"

#: Short TTL -- enough for a browser redirect round trip through Discord's
#: own consent screen, not a long-lived forgeable-looking token.
DEFAULT_TTL_SECONDS = 600


@dataclass(slots=True, frozen=True)
class PendingInstall:
    """Decoded, single-use context a bot-install `state` token was minted to carry.

    `client_secret`/`bot_token` are already decrypted by `consume_state()`
    -- callers must never log them (`security.md` Token & Secret Hygiene).
    """

    tenant_id: int
    admin_user_id: int
    application_id: str
    client_secret: str
    bot_token: str | None
    redirect_uri: str


def _redis_client() -> Any:
    """Resolve the async Valkey/Redis client -- identical resolution order to the C3 precedent."""
    limiter = current_app.config.get(RATE_LIMITER_CONFIG_KEY)
    existing = getattr(limiter, "_redis", None) if limiter is not None else None
    if existing is not None:
        return existing

    cached = current_app.config.get(_INSTALL_REDIS_CONFIG_KEY)
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
    current_app.config[_INSTALL_REDIS_CONFIG_KEY] = client
    return client


async def create_state(
    *,
    tenant_id: int,
    admin_user_id: int,
    application_id: str,
    client_secret: str,
    bot_token: str | None,
    redirect_uri: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    """Mint a fresh single-use `state` token and stash its (encrypted) context in Redis.

    Returns the opaque token to embed in Discord's `authorize` URL --
    never the submitted credentials themselves.
    """
    token = secrets.token_urlsafe(32)
    payload = {
        "tenant_id": tenant_id,
        "admin_user_id": admin_user_id,
        "application_id": application_id,
        "client_secret": encrypt_token(client_secret),
        "bot_token": encrypt_token(bot_token) if bot_token else None,
        "redirect_uri": redirect_uri,
    }
    client = _redis_client()
    await client.set(f"{_STATE_KEY_PREFIX}{token}", json.dumps(payload), ex=ttl_seconds)
    return token


async def consume_state(state: str) -> PendingInstall | None:
    """Atomically fetch-and-delete `state`'s context; `None` if absent/expired/already used/corrupt.

    `GETDEL` makes this single-use -- a replayed `state` always misses,
    even under two concurrent callback requests. Every failure mode
    (missing, expired, malformed JSON, missing field, undecryptable
    ciphertext) returns `None` uniformly -- callers treat any `None` as a
    rejected callback, never partially trust a payload.
    """
    if not state:
        return None
    client = _redis_client()
    try:
        raw = await client.getdel(f"{_STATE_KEY_PREFIX}{state}")
    except Exception:  # noqa: BLE001 - a Redis hiccup must fail the flow, not crash it
        logger.exception("tenant_discord_install_state.consume_failed")
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        encrypted_bot_token = data.get("bot_token")
        return PendingInstall(
            tenant_id=int(data["tenant_id"]),
            admin_user_id=int(data["admin_user_id"]),
            application_id=str(data["application_id"]),
            client_secret=decrypt_value(str(data["client_secret"])),
            bot_token=decrypt_value(str(encrypted_bot_token)) if encrypted_bot_token else None,
            redirect_uri=str(data["redirect_uri"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        logger.warning("tenant_discord_install_state.malformed_payload")
        return None
    except PlatformCredentialCryptoError:
        logger.error("tenant_discord_install_state.decrypt_failed")
        return None
