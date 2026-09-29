"""HMAC-signed, single-use, short-TTL `state` tokens for the per-tenant Discord bot-install flow.

Different shape from `services/oauth_connection_state.py`'s opaque-token
Redis lookup (that module's own docstring covers why an opaque token +
server-side Redis payload is right for the Community Connections flow).
This flow's task brief specifically calls for an HMAC-signed nonce bound
to `tenant_id` + the admin's own session (`sub`), so the token is
self-contained: signature verification alone rejects a forged or tampered
`state` with no round trip, and a short `exp` claim rejects an expired one
-- Redis is used only for the narrower single-use check (has this
specific nonce been consumed before), via `SET NX` on the nonce's SHA-256
hash (never the raw nonce -- belt-and-suspenders since the nonce is
single-use either way, but avoids ever persisting the literal bytes that
appeared in a browser-visible URL).

`_redis_client()` mirrors `services/oauth_connection_state.py`'s own
resolution order (reuse `RateLimiter`'s open connection; fall back to a
lazily-opened client against `HubAPIConfig.valkey_url`) -- copied rather
than imported since that module's client is private to its own opaque-
token keyspace and this flow's key prefix is deliberately different.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, cast

from quart import current_app

from config import HubAPIConfig
from services.rate_limiting import RATE_LIMITER_CONFIG_KEY

logger = logging.getLogger(__name__)

#: Redis key prefix a consumed nonce's hash is recorded under.
_NONCE_KEY_PREFIX = "oauth:guild:install:nonce:"

#: `app.config` key a lazily-opened raw Valkey client is cached under.
_GUILD_OAUTH_REDIS_CONFIG_KEY = "guild_oauth_state_redis"

#: Short TTL, per the task brief -- 10 minutes is enough for a browser
#: redirect round trip through Discord's own consent screen without
#: leaving a long-lived forgeable-looking token around.
DEFAULT_TTL_SECONDS = 600


class StateError(ValueError):
    """Raised by `verify_and_consume_state()` for any forged/expired/replayed/malformed state."""


@dataclass(slots=True, frozen=True)
class StatePayload:
    """Decoded, verified context an install `state` token was minted to carry."""

    tenant_id: int
    admin_user_id: int
    nonce: str
    issued_at: int
    expires_at: int


def _secret() -> bytes:
    """HMAC signing key -- a dedicated env var, fails closed if unset.

    Deliberately separate from `HubAPIConfig.secret_key` (JWT HS256
    signing) -- a different security domain (short-lived OAuth CSRF
    tokens vs. session JWTs), same separation-of-keys rationale as
    `services/tenant_platform_credentials_crypto.py`'s own dedicated key.
    """
    key = os.environ.get("GUILD_OAUTH_STATE_SECRET", "")
    if not key:
        raise StateError("GUILD_OAUTH_STATE_SECRET is not configured")
    return key.encode("utf-8")


def _redis_client() -> Any:
    limiter = current_app.config.get(RATE_LIMITER_CONFIG_KEY)
    existing = getattr(limiter, "_redis", None) if limiter is not None else None
    if existing is not None:
        return existing

    cached = current_app.config.get(_GUILD_OAUTH_REDIS_CONFIG_KEY)
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
    current_app.config[_GUILD_OAUTH_REDIS_CONFIG_KEY] = client
    return client


def mint_state(
    *, tenant_id: int, admin_user_id: int, ttl_seconds: int = DEFAULT_TTL_SECONDS
) -> str:
    """Build a signed, opaque `state` string embedding `tenant_id` + the admin's session.

    Format: `base64url(payload_json).hex(hmac_sha256(payload_json))` --
    self-contained, no server-side write at mint time (the single-use
    check happens at consume time, in `verify_and_consume_state()`).
    """
    now = int(time.time())
    payload = {
        "tenant_id": tenant_id,
        "admin_user_id": admin_user_id,
        "nonce": secrets.token_urlsafe(24),
        "iat": now,
        "exp": now + ttl_seconds,
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(_secret(), payload_json, hashlib.sha256).hexdigest()
    encoded = base64.urlsafe_b64encode(payload_json).rstrip(b"=").decode("ascii")
    return f"{encoded}.{signature}"


async def verify_and_consume_state(state: str) -> StatePayload:
    """Verify signature + expiry, then atomically consume the nonce (single-use).

    Raises `StateError` for anything forged, malformed, expired, or
    already-consumed (a replay) -- callers must treat every `StateError`
    as a rejected install callback, never partially trust the payload.
    """
    if not state or "." not in state:
        raise StateError("malformed state")

    encoded, _, signature = state.partition(".")
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload_json = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise StateError("malformed state encoding") from exc

    expected_signature = hmac.new(_secret(), payload_json, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        raise StateError("forged or tampered state signature")

    try:
        data = json.loads(payload_json)
        tenant_id = int(data["tenant_id"])
        admin_user_id = int(data["admin_user_id"])
        nonce = str(data["nonce"])
        issued_at = int(data["iat"])
        expires_at = int(data["exp"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StateError("malformed state payload") from exc

    if int(time.time()) > expires_at:
        raise StateError("expired state")

    nonce_hash = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    client = _redis_client()
    remaining_ttl = max(expires_at - int(time.time()), 1)
    try:
        reserved = await client.set(
            f"{_NONCE_KEY_PREFIX}{nonce_hash}", "1", ex=remaining_ttl, nx=True
        )
    except Exception as exc:  # noqa: BLE001 - a Redis hiccup must fail the flow, not crash it
        logger.exception("guild_oauth_state.consume_failed")
        raise StateError("could not verify single-use nonce") from exc
    if not reserved:
        raise StateError("replayed state -- nonce already consumed")

    return StatePayload(
        tenant_id=tenant_id,
        admin_user_id=admin_user_id,
        nonce=nonce,
        issued_at=issued_at,
        expires_at=expires_at,
    )
