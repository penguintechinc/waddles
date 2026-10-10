"""Single-use login-flow state and SAML assertion replay cache (Redis/Valkey).

Mirrors `services/oauth_connection_state.py`'s design for the same reasons
(hub-api runs several replicas, so an in-process single-use set would be
invisible to whichever pod the IdP's redirect happens to land on): the state
token is an unguessable `secrets.token_urlsafe(32)` Redis key suffix, the
payload never leaves hub-api's infra, and `GETDEL` makes consumption atomic and
single-use even under concurrent callbacks.

The payload carries what the callback needs to finish *this specific* flow and
nothing the browser can influence: the connection, the OIDC `nonce` and PKCE
`code_verifier`, or the SAML `AuthnRequest` ID the response must answer
(`InResponseTo`). A missing/expired/reused state is simply `None`; the caller
treats that as a failed login.

`remember_assertion` is defence in depth for SAML: even though a consumed state
already makes a replayed `Response` fail its `InResponseTo` check, the
assertion ID is also recorded `SET NX` until the assertion would have expired,
so the same signed assertion can never be accepted twice, under any state.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Final, cast

from quart import current_app

from config import HubAPIConfig
from services.rate_limiting import RATE_LIMITER_CONFIG_KEY
from services.sso_types import SsoIdpUnavailableError

logger = logging.getLogger(__name__)

_STATE_KEY_PREFIX: Final = "sso:state:"
_ASSERTION_KEY_PREFIX: Final = "sso:saml:assertion:"

#: `app.config` key a lazily-opened raw Valkey client is cached under (and the
#: key tests inject a fake under).
SSO_REDIS_CONFIG_KEY: Final = "sso_state_redis"


@dataclass(slots=True, frozen=True)
class SsoStatePayload:
    """Decoded, single-use context a login `state` token was minted to carry."""

    connection_public_id: str
    protocol: str
    #: OIDC/Google: the `nonce` the ID token must echo. SAML: unused (None).
    nonce: str | None = None
    #: OIDC/Google: PKCE verifier for the token exchange. SAML: unused (None).
    code_verifier: str | None = None
    #: SAML: the `AuthnRequest` ID the response's `InResponseTo` must equal.
    request_id: str | None = None


def _redis_client() -> Any:
    """Resolve the async Valkey/Redis client -- identical order to `oauth_connection_state`."""
    injected = current_app.config.get(SSO_REDIS_CONFIG_KEY)
    if injected is not None:
        return injected

    limiter = current_app.config.get(RATE_LIMITER_CONFIG_KEY)
    existing = getattr(limiter, "_redis", None) if limiter is not None else None
    if existing is not None:
        return existing

    import redis.asyncio as redis_asyncio

    cfg = cast(HubAPIConfig, current_app.config["HUB_API_CONFIG"])
    client = redis_asyncio.from_url(
        cfg.valkey_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    current_app.config[SSO_REDIS_CONFIG_KEY] = client
    return client


async def create_state(payload: SsoStatePayload, *, ttl_s: int) -> str:
    """Mint a fresh single-use `state` token storing `payload`; returns the opaque token.

    Raises `SsoIdpUnavailableError`-class failures as a loud `SsoError` if the
    store is unreachable: starting a login we cannot later validate would be
    worse than refusing to start it.
    """
    token = secrets.token_urlsafe(32)
    body = json.dumps(
        {
            "connection_public_id": payload.connection_public_id,
            "protocol": payload.protocol,
            "nonce": payload.nonce,
            "code_verifier": payload.code_verifier,
            "request_id": payload.request_id,
        }
    )
    try:
        await _redis_client().set(f"{_STATE_KEY_PREFIX}{token}", body, ex=ttl_s)
    except Exception as exc:
        # Redis driver exceptions can embed the connection URL (credentials) --
        # surface a fixed message; the type name is logged by the caller.
        raise SsoIdpUnavailableError(
            "state_store_unavailable", "SSO state store is unreachable"
        ) from exc
    return token


async def consume_state(state: str) -> SsoStatePayload | None:
    """Atomically fetch-and-delete `state`'s payload; `None` if absent/expired/already used."""
    if not state:
        return None
    try:
        raw = await _redis_client().getdel(f"{_STATE_KEY_PREFIX}{state}")
    except Exception as exc:
        raise SsoIdpUnavailableError(
            "state_store_unavailable", "SSO state store is unreachable"
        ) from exc
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return SsoStatePayload(
            connection_public_id=str(data["connection_public_id"]),
            protocol=str(data["protocol"]),
            nonce=data.get("nonce"),
            code_verifier=data.get("code_verifier"),
            request_id=data.get("request_id"),
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("sso.state.malformed_payload")
        return None


async def remember_assertion(assertion_id: str, *, ttl_s: int) -> bool:
    """Record a SAML assertion ID; return False if it was already seen (a replay).

    `SET NX EX`: only the first caller wins. `ttl_s` is clamped to >= 1 so an
    assertion that is about to expire is still remembered for the remainder of
    its validity window.
    """
    ttl = max(1, int(ttl_s))
    try:
        stored = await _redis_client().set(
            f"{_ASSERTION_KEY_PREFIX}{assertion_id}", "1", ex=ttl, nx=True
        )
    except Exception as exc:
        raise SsoIdpUnavailableError(
            "state_store_unavailable", "SSO state store is unreachable"
        ) from exc
    return bool(stored)
